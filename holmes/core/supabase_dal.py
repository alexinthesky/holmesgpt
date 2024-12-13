import base64
import json
import logging
import os
import threading
from typing import Dict, Optional, List, Tuple
from uuid import uuid4

import yaml
from holmes.core.tool_calling_llm import ResourceInstructionDocument, ResourceInstructions
from holmes.utils.definitions import RobustaConfig
from postgrest.types import ReturnMethod
from supabase import create_client
from supabase.lib.client_options import ClientOptions
from pydantic import BaseModel
from cachetools import TTLCache
from postgrest._sync.request_builder import SyncQueryRequestBuilder
from postgrest.exceptions import APIError as PGAPIError

from holmes.common.env_vars import (ROBUSTA_CONFIG_PATH, ROBUSTA_ACCOUNT_ID, STORE_URL, STORE_API_KEY, STORE_EMAIL,
                                    STORE_PASSWORD)

from datetime import datetime, timedelta

SUPABASE_TIMEOUT_SECONDS = int(os.getenv("SUPABASE_TIMEOUT_SECONDS", 3600))

ISSUES_TABLE = "Issues"
EVIDENCE_TABLE = "Evidence"
RUNBOOKS_TABLE = "HolmesRunbooks"
SESSION_TOKENS_TABLE = "AuthTokens"
HOLMES_STATUS_TABLE = "HolmesStatus"
HOLMES_TOOLSET = "HolmesToolsStatus"


class RobustaToken(BaseModel):
    store_url: str
    api_key: str
    account_id: str
    email: str
    password: str


class SupabaseDal:

    def __init__(self):
        self.enabled = self.__init_config()
        if not self.enabled:
            logging.info("Robusta store initialization parameters not provided. skipping")
            return
        logging.info(f"Initializing robusta store for account {self.account_id}")
        options = ClientOptions(postgrest_client_timeout=SUPABASE_TIMEOUT_SECONDS)
        self.client = create_client(self.url, self.api_key, options)
        self.user_id = self.sign_in()
        ttl = int(os.environ.get("SAAS_SESSION_TOKEN_TTL_SEC", "82800"))  # 23 hours
        self.patch_postgrest_execute()
        self.token_cache = TTLCache(maxsize=1, ttl=ttl)
        self.lock = threading.Lock()

    def patch_postgrest_execute(self):
        logging.info("Patching postgres execute")
        # This is somewhat hacky.
        def execute_with_retry(_self):
            try:
                return self._original_execute(_self)
            except PGAPIError as exc:
                message = exc.message or ""
                if exc.code == "PGRST301" or "expired" in message.lower():
                    # JWT expired. Sign in again and retry the query
                    logging.error("JWT token expired/invalid, signing in to Supabase again")
                    self.sign_in()
                    # update the session to the new one, after re-sign in
                    _self.session = self.client.postgrest.session
                    return self._original_execute(_self)
                else:
                    raise

        self._original_execute = SyncQueryRequestBuilder.execute
        SyncQueryRequestBuilder.execute = execute_with_retry

    @staticmethod
    def __load_robusta_config() -> Optional[RobustaToken]:
        config_file_path = ROBUSTA_CONFIG_PATH
        env_ui_token = os.environ.get("ROBUSTA_UI_TOKEN")
        if env_ui_token:
            # token provided as env var
            return RobustaToken(**json.loads(base64.b64decode(env_ui_token)))

        if not os.path.exists(config_file_path):
            logging.info(f"No robusta config in {config_file_path}")
            return None

        logging.info(f"loading config {config_file_path}")
        with open(config_file_path) as file:
            yaml_content = yaml.safe_load(file)
            config = RobustaConfig(**yaml_content)
            for conf in config.sinks_config:
                if "robusta_sink" in conf.keys():
                    token = conf["robusta_sink"].get("token")
                    return RobustaToken(**json.loads(base64.b64decode(token)))

        return None

    def __init_config(self) -> bool:
        # trying to load the supabase connection parameters from the robusta token, if exists
        # if not, using env variables as fallback
        robusta_token = self.__load_robusta_config()
        if robusta_token:
            self.account_id = robusta_token.account_id
            self.url = robusta_token.store_url
            self.api_key = robusta_token.api_key
            self.email = robusta_token.email
            self.password = robusta_token.password
        else:
            self.account_id = ROBUSTA_ACCOUNT_ID
            self.url = STORE_URL
            self.api_key = STORE_API_KEY
            self.email = STORE_EMAIL
            self.password = STORE_PASSWORD

        # valid only if all store parameters are provided
        return all([self.account_id, self.url, self.api_key, self.email, self.password])

    def sign_in(self) -> str:
        logging.info("Supabase DAL login")
        res = self.client.auth.sign_in_with_password({"email": self.email, "password": self.password})
        self.client.auth.set_session(res.session.access_token, res.session.refresh_token)
        self.client.postgrest.auth(res.session.access_token)
        return res.user.id

    def get_issue_data(self, issue_id: Optional[str]) -> Optional[Dict]:
        # TODO this could be done in a single atomic SELECT, but there is no
        # foreign key relation between Issues and Evidence.
        if not issue_id:
            return None
        if not self.enabled:  # store not initialized
            return None
        issue_data = None
        try:
            issue_response = (
                self.client
                    .table(ISSUES_TABLE)
                    .select("*")
                    .filter("id", "eq", issue_id)
                    .execute()
            )
            if len(issue_response.data):
                issue_data = issue_response.data[0]

        except Exception:  # e.g. invalid id format
            logging.exception("Supabase error while retrieving issue data")
            return None
        if not issue_data:
            return None
        evidence = (
            self.client
            .table(EVIDENCE_TABLE)
            .select("*")
            .filter("issue_id", "eq", issue_id)
            .execute()
        )
        enrichment_blacklist = {"text_file", "graph", "ai_analysis", "holmes"}
        data = [enrich for enrich in evidence.data if enrich.get("enrichment_type") not in enrichment_blacklist]

        issue_data["evidence"] = data
        return issue_data

    def get_resource_instructions(self, type: str, name: Optional[str]) -> Optional[ResourceInstructions]:
        if not self.enabled or not name:
            return None

        res = (
            self.client
            .table(RUNBOOKS_TABLE)
            .select("runbook")
            .eq("account_id", self.account_id)
            .eq("subject_type", type)
            .eq("subject_name", name)
            .execute()
        )
        if res.data:
            instructions = res.data[0].get("runbook").get("instructions")
            documents_data = res.data[0].get("runbook").get("documents")
            documents = []

            if documents_data:
                for document_data in documents_data:
                    url = document_data.get("url", None)
                    if url:
                        documents.append(ResourceInstructionDocument(url=url))
                    else:
                        logging.warning(f"Unsupported runbook for subject_type={type} / subject_name={name}: {document_data}")

            return ResourceInstructions(instructions=instructions, documents=documents)

        return None

    def create_session_token(self) -> str:
        token = str(uuid4())
        self.client.table(SESSION_TOKENS_TABLE).insert(
            {
                "account_id": self.account_id,
                "user_id": self.user_id,
                "token": token,
                "type": "HOLMES",
            }, returning=ReturnMethod.minimal  # must use this, because the user cannot read this table
        ).execute()
        return token

    def get_ai_credentials(self) -> Tuple[str, str]:
        with self.lock:
            session_token = self.token_cache.get("session_token")
            if not session_token:
                session_token = self.create_session_token()
                self.token_cache["session_token"] = session_token

        return self.account_id, session_token

    def get_workload_issues(self, resource: dict, since_hours: float) -> List[str]:
        if not self.enabled or not resource:
            return []

        cluster = resource.get("cluster")
        if not cluster:
            logging.debug("Missing workload cluster for issues.")
            return []

        since: str = (datetime.now() - timedelta(hours=since_hours)).isoformat()

        svc_key = f"{resource.get('namespace', '')}/{resource.get('kind', '')}/{resource.get('name', '')}"
        logging.debug(f"getting issues for workload {svc_key}")
        try:
            res = (
                self.client
                .table(ISSUES_TABLE)
                .select("id, creation_date, aggregation_key")
                .eq("account_id", self.account_id)
                .eq("cluster", cluster)
                .eq("service_key", svc_key)
                .gte("creation_date", since)
                .order("creation_date")
                .execute()
            )

            if not res.data:
                return []

            issue_dict = dict()
            for issue in res.data:
                issue_dict[issue.get("aggregation_key")] = issue.get("id")

            unique_issues: list[str] = list(issue_dict.values())

            res = (
                self.client
                .table(EVIDENCE_TABLE)
                .select("data, enrichment_type")
                .in_("issue_id", unique_issues)
                .execute()
            )

            enrichment_blacklist = {"text_file", "graph", "ai_analysis", "holmes"}
            data = [evidence.get("data") for evidence in res.data if evidence.get("enrichment_type") not in enrichment_blacklist]
            return data

        except:
            logging.exception("failed to fetch workload issues data", exc_info=True)
            return []

    def upsert_holmes_status(self, holmes_status_data: dict) -> None:
        updated_at = datetime.now().isoformat()
        try:
            res = (
                self.client
                .table(HOLMES_STATUS_TABLE)
                .upsert({
                    "account_id": self.account_id,
                    "updated_at": updated_at,
                    **holmes_status_data,
                },
                on_conflict='account_id, cluster_id')
                .execute()
            )
        except Exception as error:
            logging.error(f"Error happened during upserting holmes status: {error}", 
                          exc_info=True)

        return None
    
    def sync_toolsets(self, toolsets: list[dict], cluster_name: str) -> None:
        if not toolsets:
            logging.warning("No toolsets were provided for synchronization.")
            return
        
        provided_toolset_names = [toolset['toolset_name'] for toolset in toolsets]
   
        try:
            self.client.table(HOLMES_TOOLSET).upsert(
                toolsets,
                on_conflict='account_id, cluster_id, toolset_name'
            ).execute()

            logging.info("Toolsets upserted successfully.")

            
            self.client.table(HOLMES_TOOLSET).delete().eq("account_id", 
                                                                       self.account_id).eq(
                                                                           'cluster_id', cluster_name).not_.in_(
                'toolset_name', provided_toolset_names
            ).execute()

            logging.info("Toolsets synchronized successfully.")

        except Exception as e:
            logging.exception(f"An error occurred during toolset synchronization: {e}",
                              exc_info=True)
