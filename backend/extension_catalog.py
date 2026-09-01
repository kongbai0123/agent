"""Trusted catalog records for the local Extension Center.

Manifest V1 remains the only format accepted from local files.  GitHub and
Notion are intentionally described by a separate, server-owned connector
contract: adding connectors must not make the executable Manifest V1 surface
more permissive.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Mapping, Union

from pydantic import BaseModel, ConfigDict, Field

from extension_manifest import (
    EXTENSION_ID_PATTERN,
    ExtensionManifest,
    ExtensionPermission,
    canonical_manifest_bytes,
    parse_extension_manifest,
    safe_settings_identifier,
)


class ConnectorEntrypoint(BaseModel):
    """Reference to one connector adapter compiled into Workbench."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["connector"] = "connector"
    adapter: Literal["github", "notion", "gmail"]


class ConnectorExtensionDescriptor(BaseModel):
    """Server-owned descriptor kept deliberately separate from Manifest V1."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["connector-v1"] = "connector-v1"
    id: str = Field(pattern=EXTENSION_ID_PATTERN, max_length=96)
    name: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=500)
    publisher: str = Field(min_length=1, max_length=100)
    origin: Literal["builtin"] = "builtin"
    kind: Literal["connector"] = "connector"
    category: str = Field(min_length=1, max_length=64)
    entrypoint: ConnectorEntrypoint
    permissions: list[ExtensionPermission] = Field(min_length=1, max_length=32)
    health_probe: Literal["github", "notion", "gmail"]
    removable: bool = True
    default_installed: bool = False
    default_enabled: bool = False


CatalogRecord = Union[ExtensionManifest, ConnectorExtensionDescriptor]


def _permission(
    permission_id: str,
    risk: str,
    description: str,
) -> dict[str, Any]:
    return {
        "id": permission_id,
        "risk": risk,
        "description": description,
        "required": True,
    }


def _configuration_sha256(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=repr,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def catalog_record_payload(record: CatalogRecord) -> dict[str, Any]:
    return record.model_dump(mode="json", exclude_none=True)


def canonical_catalog_record_bytes(record: CatalogRecord) -> bytes:
    if isinstance(record, ExtensionManifest):
        return canonical_manifest_bytes(record)
    return json.dumps(
        catalog_record_payload(record),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def catalog_record_sha256(record: CatalogRecord) -> str:
    return hashlib.sha256(canonical_catalog_record_bytes(record)).hexdigest()


def catalog_record_contract(record: CatalogRecord) -> str:
    return "manifest-v1" if isinstance(record, ExtensionManifest) else "connector-v1"


def builtin_manifests() -> tuple[ExtensionManifest, ...]:
    common = {
        "schema_version": 1,
        "version": "1.0.0",
        "publisher": "Local AI Workbench",
        "origin": "builtin",
        "default_installed": True,
        "default_enabled": True,
        "removable": False,
    }
    definitions = (
        {
            **common,
            "id": "builtin.n8n",
            "name": "n8n",
            "description": "Local workflow automation with governed Gmail drafts.",
            "kind": "integration",
            "category": "automation",
            "entrypoint": {"type": "builtin", "adapter": "n8n"},
            "permissions": [
                _permission(
                    "network.n8n",
                    "external_write",
                    "Call the configured managed n8n service.",
                ),
            ],
            "health_probe": "n8n",
            "default_installed": False,
            "default_enabled": False,
        },
        {
            **common,
            "id": "builtin.cursor",
            "name": "Cursor Agent",
            "description": "Cursor adapter is not available in this release.",
            "kind": "integration",
            "category": "development",
            "entrypoint": {"type": "builtin", "adapter": "cursor"},
            "permissions": [
                _permission(
                    "workspace.cursor",
                    "write",
                    "Read or modify the selected project.",
                ),
            ],
            "health_probe": "cursor",
            "default_installed": False,
            "default_enabled": False,
        },
        {
            **common,
            "id": "builtin.excel",
            "name": "Microsoft Excel",
            "description": "Excel adapter is not available in this release.",
            "kind": "desktop",
            "category": "productivity",
            "entrypoint": {"type": "builtin", "adapter": "excel"},
            "permissions": [
                _permission(
                    "desktop.excel",
                    "irreversible",
                    "Read, modify, or save a bound workbook.",
                ),
            ],
            "health_probe": "excel",
            "default_installed": False,
            "default_enabled": False,
        },
        {
            **common,
            "id": "builtin.ollama",
            "name": "Ollama",
            "description": "Use the configured local Ollama model service.",
            "kind": "model_provider",
            "category": "models",
            "entrypoint": {"type": "builtin", "adapter": "ollama"},
            "permissions": [
                _permission(
                    "network.ollama",
                    "external_read",
                    "Call the configured local Ollama API.",
                ),
            ],
            "health_probe": "ollama",
        },
    )
    return tuple(parse_extension_manifest(item) for item in definitions)


def builtin_connector_descriptors() -> tuple[ConnectorExtensionDescriptor, ...]:
    definitions = (
        {
            "id": "connector.github",
            "name": "GitHub",
            "version": "1.0.0",
            "description": (
                "Read repositories, issues, pull requests, and checks; approved "
                "writes can create or update issues and add conversation comments."
            ),
            "publisher": "Local AI Workbench",
            "category": "development",
            "entrypoint": {"adapter": "github"},
            "permissions": [
                _permission(
                    "connector.github.repository.read",
                    "external_read",
                    "Read project-bound repository content and collaboration metadata.",
                ),
                _permission(
                    "connector.github.issue.write",
                    "external_write",
                    "Create or update issues and add approved conversation comments.",
                ),
            ],
            "health_probe": "github",
        },
        {
            "id": "connector.notion",
            "name": "Notion",
            "version": "1.0.0",
            "description": (
                "Read project-bound Notion roots; approved writes can create or "
                "update pages and append blocks."
            ),
            "publisher": "Local AI Workbench",
            "category": "productivity",
            "entrypoint": {"adapter": "notion"},
            "permissions": [
                _permission(
                    "connector.notion.content.read",
                    "external_read",
                    "Read selected page and database roots and their descendants.",
                ),
                _permission(
                    "connector.notion.content.write",
                    "external_write",
                    "Create or update content after per-operation approval.",
                ),
            ],
            "health_probe": "notion",
        },
        {
            "id": "connector.gmail",
            "name": "Gmail",
            "version": "1.0.0",
            "description": "搜尋及閱讀信件，並在逐次核准後建立或寄送草稿。",
            "publisher": "Local AI Workbench",
            "category": "productivity",
            "entrypoint": {"adapter": "gmail"},
            "permissions": [
                _permission(
                    "connector.gmail.message.read",
                    "external_read",
                    "讀取目前專案已授權 Gmail 帳號中的信件。",
                ),
                _permission(
                    "connector.gmail.draft.write",
                    "external_write",
                    "建立或寄送 Gmail 草稿；每次操作都受權限政策與核准治理。",
                ),
            ],
            "health_probe": "gmail",
        },
    )
    return tuple(ConnectorExtensionDescriptor.model_validate(item) for item in definitions)


def builtin_catalog_records() -> tuple[CatalogRecord, ...]:
    return (*builtin_manifests(), *builtin_connector_descriptors())


_BUILTIN_METADATA: dict[str, dict[str, Any]] = {
    "builtin.n8n": {
        "runtime_available": True,
        "connection_required": False,
        "capabilities": ["workflow_automation", "gmail_governed_drafts"],
        "documentation": {
            "summary": "建立與執行本機自動化流程，並讓 Gmail 草稿、寄送等外部動作保留人工核准。",
            "overview": "n8n 負責串接外部服務與執行多步驟流程；Workbench 負責專案範圍、權限、核准、狀態與稽核紀錄。",
            "common_tasks": [
                {"title": "管理工作流程", "description": "在工作流程主介面建立、整理、啟動與檢查 n8n 工作流程。"},
                {"title": "Gmail 草稿治理", "description": "接收郵件事件、產生草稿，經使用者檢查後才允許寄送。"},
            ],
            "data_handling": "資料送往你設定的本機 n8n 服務；憑證與郵件內容仍受 Workbench 的秘密儲存與保留政策管理。",
            "approval_policy": "外部寄送與具副作用的步驟必須依既有流程取得人工核准。",
            "limitations": ["節點、憑證與流程治理的詳細資料請到工作流程介面查看。"],
        },
    },
    "builtin.cursor": {
        "runtime_available": False,
        "availability_reason": "cursor_adapter_not_implemented",
        "connection_required": False,
        "capabilities": [],
        "documentation": {
            "summary": "預留的 Cursor Agent 整合入口；目前版本尚未提供可執行的介接器。",
            "overview": "未來可用來把專案內容與受治理的開發任務交給 Cursor；目前不會啟動程序或修改檔案。",
            "common_tasks": [],
            "data_handling": "目前不會傳送或處理任何專案資料。",
            "approval_policy": "功能尚不可用，無法授權執行。",
            "limitations": ["此版本缺少 Cursor 介接器。"],
        },
    },
    "builtin.excel": {
        "runtime_available": False,
        "availability_reason": "excel_adapter_not_implemented",
        "connection_required": False,
        "capabilities": [],
        "documentation": {
            "summary": "預留的 Microsoft Excel 桌面整合入口；目前版本尚未提供介接器。",
            "overview": "未來可在明確綁定活頁簿後讀取、修改與儲存資料；目前不會操作 Excel。",
            "common_tasks": [],
            "data_handling": "目前不會讀取或修改任何活頁簿。",
            "approval_policy": "功能尚不可用，無法授權執行。",
            "limitations": ["此版本缺少 Excel 介接器。"],
        },
    },
    "builtin.ollama": {
        "runtime_available": True,
        "connection_required": False,
        "capabilities": ["local_models"],
        "documentation": {
            "summary": "讓 Workbench 使用本機 Ollama 模型完成聊天、規劃與支援能力驗證的工具工作。",
            "overview": "Workbench 透過你設定的本機回環 Ollama 端點列出並呼叫模型；模型檔與推論都留在本機。",
            "common_tasks": [
                {"title": "本機對話", "description": "使用已安裝的 Ollama 聊天模型產生回答。"},
                {"title": "工具型 Agent", "description": "只有通過工具能力驗證的模型才會收到工具規格。"},
            ],
            "data_handling": "Prompt 與模型輸出送往設定的本機 Ollama 服務，不會因此外掛自動上傳雲端。",
            "approval_policy": "模型本身不會放寬工具、專案或外部寫入的固定核准政策。",
            "limitations": ["實際能力取決於已安裝模型、context 大小及工具驗證結果。"],
        },
    },
    "connector.github": {
        "runtime_available": True,
        "connection_required": True,
        "connector_id": "github",
        "capabilities": ["repositories", "issues", "pull_requests", "checks"],
        "documentation": {
            "summary": "讀取專案已綁定的 GitHub 儲存庫、議題、拉取請求與檢查結果，並在你核准後管理討論。",
            "overview": "GitHub 連接器使用你自己的 GitHub App 連線；Agent 只會看到目前專案已綁定的儲存庫。",
            "common_tasks": [
                {"title": "讀取開發內容", "description": "查詢儲存庫、檔案、提交、議題、拉取請求與檢查結果。"},
                {"title": "協作寫入", "description": "經逐次核准後建立或更新議題，或在議題與拉取請求中加入一般留言。"},
            ],
            "data_handling": "內容會在 Workbench 與 GitHub API 之間傳送；存取權杖會加密保存，每次執行前都會重新檢查專案與儲存庫允許清單。",
            "approval_policy": "讀取在專案授權範圍內可直接執行；所有 GitHub 寫入逐次要求人工核准。",
            "limitations": ["禁止修改程式碼、建立分支或拉取請求、合併、刪除，以及變更儲存庫設定。"],
        },
    },
    "connector.notion": {
        "runtime_available": True,
        "connection_required": True,
        "connector_id": "notion",
        "capabilities": ["pages", "databases", "blocks"],
        "documentation": {
            "summary": "讀取專案已綁定的 Notion 頁面或資料庫，並在你核准後建立或更新內容。",
            "overview": "Notion 連接器只會存取你已授權且綁定到目前專案的根頁面、資料庫及其子項。",
            "common_tasks": [
                {"title": "知識查詢", "description": "搜尋、讀取與摘要已授權的頁面、資料庫與內容區塊。"},
                {"title": "內容更新", "description": "經逐次核准後建立頁面、更新頁面或附加內容區塊。"},
            ],
            "data_handling": "內容會在 Workbench 與 Notion API 之間傳送；存取權杖會加密保存，執行前會驗證專案根範圍與父子關係。",
            "approval_policy": "讀取在綁定範圍內可直接執行；所有 Notion 寫入逐次要求人工核准。",
            "limitations": ["禁止刪除、封存與留言；未綁定的頁面或資料庫不會提供給 Agent。"],
        },
    },
    "connector.gmail": {
        "runtime_available": True,
        "connection_required": True,
        "connector_id": "gmail",
        "capabilities": ["message_search", "message_read", "draft_create", "draft_send"],
        "documentation": {
            "summary": "連接你的 Gmail 帳號，讓 Agent 在目前專案內搜尋與閱讀郵件，並在你核准後建立或寄送草稿。",
            "overview": "導入後會開啟 Google 帳號授權。Workbench 只保存加密權杖，不保存你的 Google 密碼；Agent 只會在已綁定的 Project 中取得這個信箱工具。",
            "common_tasks": [
                {"title": "搜尋與閱讀郵件", "description": "依寄件者、日期、標籤或關鍵字搜尋，再讀取指定信件內容。"},
                {"title": "建立草稿", "description": "依對話內容整理收件者、主旨與正文，取得你的批准後存入 Gmail 草稿。"},
                {"title": "寄送草稿", "description": "只有在再次確認草稿 ID 並取得逐次批准後，才會要求 Gmail 寄出。"},
            ],
            "data_handling": "郵件查詢與必要內容會傳送給目前 Agent 使用的模型；OAuth Client Secret 與權杖只存於本機加密保管庫。",
            "approval_policy": "搜尋與閱讀可依 Project 範圍直接執行；建立草稿及寄送屬外部寫入，必須依目前權限等級取得批准。",
            "limitations": ["首次導入需使用你自己的 Google OAuth 應用程式資料。", "目前不支援刪除郵件、修改標籤或變更 Gmail 設定。"],
        },
    },
}

_EN_BUILTIN_DOCUMENTATION: dict[str, dict[str, Any]] = {
    "builtin.n8n": {
        "summary": "Build and run local automation workflows while keeping governed Gmail drafts and external actions behind human approval.",
        "overview": "n8n connects services and runs multi-step workflows. Workbench controls project scope, permissions, approval, status, and audit records.",
        "common_tasks": [
            {"title": "Manage workflows", "description": "Create, organize, start, and inspect n8n workflows in the workflow workspace."},
            {"title": "Govern Gmail drafts", "description": "Receive mail events, prepare drafts, and send only after user review."},
        ],
        "data_handling": "Data is sent to the configured local n8n service. Credentials and mail content remain subject to Workbench secret-storage and retention policies.",
        "approval_policy": "Sending messages and other side-effecting workflow steps require human approval under the active policy.",
        "limitations": ["Open the workflow workspace for node, credential, and workflow governance details."],
    },
    "builtin.cursor": {
        "summary": "Reserved integration for Cursor Agent. This release does not include an executable adapter.",
        "overview": "A future adapter may delegate governed development work to Cursor. The current entry cannot start a process or modify files.",
        "common_tasks": [],
        "data_handling": "No project data is read or sent in this release.",
        "approval_policy": "The unavailable adapter cannot be authorized to run.",
        "limitations": ["The Cursor adapter is not implemented."],
    },
    "builtin.excel": {
        "summary": "Reserved integration for Microsoft Excel. This release does not include an adapter.",
        "overview": "A future adapter may read, edit, and save explicitly bound workbooks. The current entry cannot control Excel.",
        "common_tasks": [],
        "data_handling": "No workbook is read or modified in this release.",
        "approval_policy": "The unavailable adapter cannot be authorized to run.",
        "limitations": ["The Excel adapter is not implemented."],
    },
    "builtin.ollama": {
        "summary": "Use local Ollama models for chat, planning, and tool work when the selected model passes capability verification.",
        "overview": "Workbench lists and calls models through the configured loopback Ollama endpoint, keeping model files and inference local.",
        "common_tasks": [
            {"title": "Local chat", "description": "Generate answers with an installed Ollama chat model."},
            {"title": "Tool-enabled agent", "description": "Only models that pass tool verification receive tool schemas."},
        ],
        "data_handling": "Prompts and outputs are sent to the configured local Ollama service and are not uploaded to a cloud service by this extension.",
        "approval_policy": "The model cannot relax tool, project, or external-write approval rules.",
        "limitations": ["Capabilities depend on the installed model, context capacity, and verification results."],
    },
    "connector.github": {
        "summary": "Read code, issues, pull requests, and checks in project-bound repositories, then manage discussions after approval.",
        "overview": "The GitHub connector uses your GitHub App connection. The Agent sees only repositories bound to the active project.",
        "common_tasks": [
            {"title": "Read development data", "description": "Inspect repositories, files, commits, issues, pull requests, and check runs."},
            {"title": "Approved collaboration", "description": "Create or update issues and add ordinary issue or pull-request comments after approval."},
        ],
        "data_handling": "Content moves between Workbench and the GitHub API. Tokens are encrypted, and repository scope is checked before each tool call.",
        "approval_policy": "Reads inside the bound scope may run directly. Every GitHub write requires per-operation approval.",
        "limitations": ["Code changes, branches, pull-request creation, merges, deletion, and repository settings are prohibited."],
    },
    "connector.notion": {
        "summary": "Read project-bound Notion pages and databases, then create or update content after approval.",
        "overview": "The connector accesses only roots authorized by the user and bound to the active project, including permitted descendants.",
        "common_tasks": [
            {"title": "Knowledge lookup", "description": "Search, read, and summarize authorized pages, databases, and blocks."},
            {"title": "Approved updates", "description": "Create pages, update pages, or append blocks after approval."},
        ],
        "data_handling": "Content moves between Workbench and the Notion API. Tokens are encrypted, and root ancestry is checked before execution.",
        "approval_policy": "Reads inside the bound scope may run directly. Every Notion write requires per-operation approval.",
        "limitations": ["Deletion, archiving, and comments are prohibited. Unbound content is never exposed to the Agent."],
    },
    "connector.gmail": {
        "summary": "Connect Gmail so the Agent can search and read project-authorized mail, then create or send drafts after approval.",
        "overview": "Import opens Google account authorization. Workbench stores encrypted OAuth tokens, never the Google password, and exposes the mailbox only to bound projects.",
        "common_tasks": [
            {"title": "Search and read mail", "description": "Find messages by sender, date, label, or keyword and read selected results."},
            {"title": "Create drafts", "description": "Prepare recipients, a subject, and content, then create the Gmail draft after approval."},
            {"title": "Send drafts", "description": "Send a specific draft only after a separate per-operation approval."},
        ],
        "data_handling": "Mail queries and required content are sent to the active Agent model. OAuth secrets and tokens remain in the encrypted local vault.",
        "approval_policy": "Reads inside the project scope may run directly. Draft creation and sending are governed external writes.",
        "limitations": ["Initial setup uses your own Google OAuth application.", "Deletion, label changes, and Gmail settings are not supported."],
    },
}


def catalog_metadata(extension_id: str, locale: str = "zh-TW") -> dict[str, Any]:
    """Return UI/runtime metadata that is not part of an executable manifest."""

    metadata = {
        "runtime_available": True,
        "availability_reason": None,
        "connection_required": False,
        "capabilities": [],
        "documentation": None,
        **_BUILTIN_METADATA.get(extension_id, {}),
    }
    if locale == "en-US" and extension_id in _EN_BUILTIN_DOCUMENTATION:
        metadata["documentation"] = _EN_BUILTIN_DOCUMENTATION[extension_id]
    return metadata


_MCP_TOOL_DOCUMENTATION: dict[str, tuple[str, str]] = {
    "browser_navigate": ("開啟網站", "前往指定網址；可用於搜尋、開啟官方文件或切換頁面。"),
    "browser_navigate_back": ("返回上一頁", "回到目前頁籤的上一個瀏覽紀錄。"),
    "browser_snapshot": ("讀取頁面結構", "取得可存取性頁面快照，供 Agent 理解文字、連結與控制項。"),
    "browser_find": ("搜尋頁面內容", "在目前頁面的可存取性結構中尋找文字或規則。"),
    "browser_wait_for": ("等待頁面狀態", "等待文字出現、消失或短暫等待頁面完成更新。"),
    "browser_click": ("點擊頁面", "點擊已由頁面快照識別的按鈕、連結或控制項。"),
    "browser_type": ("輸入文字", "在指定欄位輸入文字；可能把內容送到目前網站。"),
    "browser_press_key": ("按下按鍵", "在瀏覽器中送出 Enter、方向鍵或其他鍵盤操作。"),
    "browser_fill_form": ("填寫表單", "一次填入多個表單欄位。"),
    "browser_select_option": ("選擇選項", "操作網頁中的下拉選單。"),
    "browser_handle_dialog": ("處理對話框", "接受或取消網站顯示的 JavaScript 對話框。"),
    "browser_tabs": ("管理頁籤", "列出、建立、切換或關閉隔離瀏覽器頁籤。"),
    "browser_close": ("關閉頁面", "關閉目前的隔離瀏覽器頁面。"),
}

_MCP_TOOL_DOCUMENTATION_EN: dict[str, tuple[str, str]] = {
    "browser_navigate": ("Open a website", "Navigate to a URL for search, documentation, or page access."),
    "browser_navigate_back": ("Go back", "Return to the previous history entry in the active tab."),
    "browser_snapshot": ("Read page structure", "Capture the accessibility tree so the Agent can understand text, links, and controls."),
    "browser_find": ("Find page content", "Search the current accessibility snapshot for text or a pattern."),
    "browser_wait_for": ("Wait for page state", "Wait for text to appear or disappear, or briefly wait for an update."),
    "browser_click": ("Click a page control", "Click a button, link, or control identified from the page snapshot."),
    "browser_type": ("Type text", "Type into a field. The text may be sent to the active website."),
    "browser_press_key": ("Press a key", "Send Enter, arrow keys, or another keyboard action to the browser."),
    "browser_fill_form": ("Fill a form", "Fill multiple form fields in one operation."),
    "browser_select_option": ("Select an option", "Choose an item in a web dropdown."),
    "browser_handle_dialog": ("Handle a dialog", "Accept or dismiss a JavaScript dialog shown by the website."),
    "browser_tabs": ("Manage tabs", "List, create, select, or close isolated browser tabs."),
    "browser_close": ("Close the page", "Close the active isolated browser page."),
}


def settings_extension_documentation(
    manifest: ExtensionManifest,
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    """Build non-secret explanatory metadata for settings-backed extensions."""

    entrypoint = manifest.entrypoint
    if entrypoint.type == "mcp_settings":
        locale = str(settings.get("ui_language") or "zh-TW")
        english = locale == "en-US"
        item = next(
            (
                raw
                for raw in settings.get("mcp_servers") or []
                if isinstance(raw, Mapping)
                and str(raw.get("id") or "").strip() == str(entrypoint.settings_id or "")
            ),
            {},
        )
        policies = item.get("tool_policies") or item.get("tools") or {}
        tools: list[dict[str, Any]] = []
        if isinstance(policies, Mapping):
            for name, policy in sorted(policies.items(), key=lambda pair: str(pair[0])):
                if not isinstance(policy, Mapping):
                    continue
                labels = _MCP_TOOL_DOCUMENTATION_EN if english else _MCP_TOOL_DOCUMENTATION
                label, description = labels.get(
                    str(name),
                    (
                        str(name).replace("_", " "),
                        "A tool provided by this local MCP server and explicitly allowed by policy."
                        if english
                else "由此本機 MCP 服務提供並經明確允許的工具。",
                    ),
                )
                tools.append(
                    {
                        "name": str(name),
                        "label": label,
                        "description": description,
                        "access": str(policy.get("access") or "read"),
                        "risk": str(policy.get("risk_level") or "read"),
                    }
                )
        playwright = str(entrypoint.settings_id or "") == "browser-playwright"
        if english:
            return {
                "summary": (
                    "Let the Agent open a separate Chrome session, search websites, and read pages. It does not use your everyday Chrome sign-in."
                    if playwright
                    else "Provide explicitly reviewed tools to the Agent through a trusted local MCP process."
                ),
                "overview": (
                    "When you ask for web research, the Agent opens an isolated Chrome session, reads the page accessibility structure, and returns the result to the conversation."
                    if playwright
                    else "Workbench starts the MCP server over local stdio and exposes only tools listed in the reviewed policy."
                ),
                "common_tasks": (
                    [
                        {"title": "Search and summarize", "description": "Open a search result or website, read its text and links, and summarize what was found."},
                        {"title": "Controlled web interaction", "description": "Click, type, select form options, or manage tabs after user approval."},
                    ]
                    if playwright
                    else [{"title": "Agent tool use", "description": "A tool-capable model calls an allowed MCP tool when the task requires it."}]
                ),
                "data_handling": (
                    "The isolated Chrome session sends requests directly to websites. Workbench receives page structure and tool results. File upload, arbitrary JavaScript, and full network-body tools are not enabled."
                    if playwright
                    else "The MCP server runs in a separate local process. Tool inputs and results still pass validation, redaction, size limits, and audit logging."
                ),
                "approval_policy": (
                    "Read-only browsing may run directly. Before any click, text entry, form action, dialog response, or tab change, Workbench shows the target, input summary, possible consequences, reversibility, and exact one-time authorization scope. Rejecting the request performs no action."
                    if playwright
                    else "Each tool is classified as read or write. Write tools remain subject to Workbench human-approval policy."
                ),
                "limitations": (
                    [
                        "Select a project and use a chat model that passed tool verification.",
                        "The isolated browser does not inherit everyday Chrome cookies, extensions, or history.",
                        "Process isolation limits failures but is not a complete Windows OS sandbox.",
                    ]
                    if playwright
                    else ["Tools are exposed only when the extension is installed, trusted, enabled, healthy, and allowed for the project.", "A separate process is not a complete OS sandbox."]
                ),
                "tools": tools,
                "runtime": {
                    "transport": str(item.get("transport") or "stdio"),
                    "tool_count": len(tools),
                    "timeout_seconds": float(item.get("timeout_seconds") or 30),
                    "profile": "Isolated and discarded on exit" if playwright and "--isolated" in (item.get("argv") or []) else "Managed by local settings",
                    "automatic_download": False,
                },
            }
        return {
            "summary": (
                "讓 Agent 代你開啟一個獨立的 Chrome 視窗、搜尋網站並讀回內容；不會使用你平常 Chrome 的登入狀態。"
                if playwright
                else "透過受信任的本機 MCP 程序，向 Agent 提供經逐項審查的工具。"
            ),
            "overview": (
                "例如你在聊天中說「搜尋 n8n 官方文件並整理重點」，Agent 會開啟隔離瀏覽器、讀取搜尋結果與頁面內容，再把整理結果帶回聊天。它不是另一個模型，也不會接管你日常使用的 Chrome。"
                if playwright
                else "Workbench 以本機 stdio 啟動獨立 MCP 程序，只註冊設定中明確列出的工具；未列出的 discovery tools 不會提供給 Agent。"
            ),
            "common_tasks": (
                [
                    {"title": "你可以直接這樣說", "description": "「搜尋 n8n 官方文件並整理安裝步驟」或「打開這個網站並找出價格頁」。"},
                    {"title": "需要操作網站時", "description": "Agent 會先說明準備點擊或輸入什麼，取得你的同意後才繼續。"},
                ]
                if playwright
                else [{"title": "Agent 工具調度", "description": "由支援 Tools 的模型依任務需要呼叫已允許的 MCP 工具。"}]
            ),
            "data_handling": (
                "你要求開啟的網址、搜尋詞與輸入內容會送到該網站；Workbench 只取回頁面結構與操作結果。關閉後不保存瀏覽器個人資料，也不開放檔案上傳或任意程式碼執行。"
                if playwright
                else "MCP 在獨立本機程序中執行；輸入與結果仍會經 ToolDispatcher 驗證、遮罩、大小限制與 Audit。"
            ),
            "approval_policy": (
                "開啟、閱讀與搜尋頁面可直接進行。點擊、輸入、表單、網站對話框與頁籤變更前，Workbench 會列出操作目標、輸入摘要、可能後果、能否復原與精確的一次性授權範圍；拒絕後不會執行該操作。"
                if playwright
                else "讀寫行為依每個 tool policy 分級；寫入工具必須遵守 Workbench 的固定人工核准政策。"
            ),
            "limitations": (
                [
                    "聊天必須先選擇專案，而且主要模型必須通過工具能力驗證。",
                    "隔離瀏覽器不會沿用日常 Chrome 的登入狀態、擴充功能或瀏覽紀錄。需要登入的網站必須在隔離視窗中另行登入。",
                    "獨立程序可避免單一工具故障拖垮 Agent，但不是完整的 Windows 系統沙箱。",
                ]
                if playwright
                else ["只有已安裝、信任、啟用且健康的專案範圍會取得工具。", "本機程序隔離不等同完整 OS Sandbox。"]
            ),
            "tools": tools,
            "runtime": {
                "transport": str(item.get("transport") or "stdio"),
                "tool_count": len(tools),
                "timeout_seconds": float(item.get("timeout_seconds") or 30),
                "profile": "隔離、結束後不保存" if playwright and "--isolated" in (item.get("argv") or []) else "由本機設定管理",
                "automatic_download": False,
            },
        }
    if entrypoint.type == "provider_settings":
        if str(settings.get("ui_language") or "zh-TW") == "en-US":
            return {
                "summary": "Use a configured model provider under capability, health, budget, and project-routing policies.",
                "overview": "This extension represents one model API connection. Dedicated OCR, embedding, and reranking models are not treated as primary chat models.",
                "common_tasks": [{"title": "Model inference", "description": "Provide chat or a dedicated processing stage according to verified model capabilities."}],
                "data_handling": "Prompts, attachments, or dedicated-model inputs may be sent to the configured provider. API keys remain in secret storage and never appear in the manifest.",
                "approval_policy": "Cross-provider routing, cloud data transfer, and dedicated capabilities remain subject to project routing and first-use consent.",
                "limitations": ["Availability depends on capability tests, health, budget, and provider permissions."],
                "tools": [],
            }
        return {
            "summary": "讓 Workbench 使用已設定的模型供應商，並依模型能力、健康、預算與專案政策決定是否可用。",
                "overview": "此擴充代表一筆模型 API 連線；它不會讓專用的文字辨識、向量嵌入或重新排序模型成為主要聊天模型。",
            "common_tasks": [{"title": "模型推論", "description": "依已驗證的模型能力提供聊天或專用處理階段。"}],
                "data_handling": "提示詞、附件或專用能力輸入可能送往你設定的供應商；API 金鑰由秘密儲存管理，不會出現在擴充資訊清單中。",
            "approval_policy": "跨供應商、資料上雲與專用能力仍受專案路由政策與首次同意限制。",
            "limitations": ["實際可用功能取決於模型能力測試、健康狀態、預算與供應商權限。"],
            "tools": [],
        }
    return {}


def mcp_configuration_payload(item: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical, non-secret MCP settings covered by Manifest V1 trust.

    Older settings only contained the original fields below, so optional
    security fields are included only when configured.  Adding an executable
    attestation, environment reference, protocol version or Tool Policy then
    changes the manifest digest and requires an explicit re-trust.
    """

    command = item.get("command") or []
    if isinstance(command, str):
        command = [command]
    environment = item.get("environment") or {}
    environment_keys = set(str(key) for key in (item.get("environment_keys") or []))
    if isinstance(environment, Mapping):
        environment_keys.update(str(key) for key in environment)
    payload: dict[str, Any] = {
        "transport": str(item.get("transport") or "stdio"),
        "executable": str(item.get("executable") or ""),
        "command": [str(part) for part in command],
        "argv": [str(part) for part in item.get("argv") or []],
        "cwd": str(item.get("cwd") or ""),
        "allowed_cwd_roots": [
            str(path) for path in item.get("allowed_cwd_roots") or []
        ],
        "environment_keys": sorted(environment_keys),
        "secret_aliases": dict(item.get("secret_aliases") or {}),
        "timeout_seconds": float(item.get("timeout_seconds") or 30),
    }
    executable_digest = item.get("expected_executable_sha256") or item.get(
        "executable_sha256"
    )
    if executable_digest is not None:
        payload["expected_executable_sha256"] = str(executable_digest)
    if "startup_timeout_seconds" in item:
        payload["startup_timeout_seconds"] = float(item["startup_timeout_seconds"])
    if "protocol_version" in item:
        payload["protocol_version"] = str(item["protocol_version"])
    if isinstance(environment, Mapping) and environment:
        payload["environment"] = {
            str(key): dict(value) if isinstance(value, Mapping) else value
            for key, value in sorted(environment.items(), key=lambda pair: str(pair[0]))
        }
    policies = item.get("tool_policies")
    if policies is None:
        policies = item.get("tools")
    if isinstance(policies, Mapping) and policies:
        payload["tool_policies"] = {
            str(key): dict(value) if isinstance(value, Mapping) else value
            for key, value in sorted(policies.items(), key=lambda pair: str(pair[0]))
        }
    return payload


def settings_manifests(settings: Mapping[str, Any]) -> list[ExtensionManifest]:
    manifests: list[ExtensionManifest] = []
    for item in settings.get("mcp_servers") or []:
        if not isinstance(item, Mapping) or not item.get("id"):
            continue
        settings_id = str(item["id"]).strip()
        safe_id = safe_settings_identifier(settings_id)
        config_digest = _configuration_sha256(mcp_configuration_payload(item))
        manifests.append(
            parse_extension_manifest(
                {
                    "schema_version": 1,
                    "id": f"mcp.{safe_id}",
                    "name": str(item.get("label") or settings_id)[:80],
                    "version": "settings-v1",
                    "description": "Trusted local MCP server configured in Workbench settings.",
                    "publisher": "Local configuration",
                    "origin": "local",
                    "kind": "mcp",
                    "category": "tools",
                    "entrypoint": {
                        "type": "mcp_settings",
                        "adapter": "mcp",
                        "settings_id": settings_id,
                        "configuration_sha256": config_digest,
                    },
                    "permissions": [
                        _permission(
                            "process.mcp",
                            "system",
                            "Start the configured MCP process and call its tools.",
                        ),
                    ],
                    "health_probe": "mcp",
                    "removable": True,
                    "default_installed": True,
                    "default_enabled": False,
                }
            )
        )
    manifests.extend(_provider_manifests(settings))
    return manifests


def _provider_manifests(settings: Mapping[str, Any]) -> list[ExtensionManifest]:
    manifests: list[ExtensionManifest] = []
    provider_items = list(settings.get("model_providers") or [])
    if (
        not provider_items
        and str(settings.get("model_provider") or "ollama").casefold()
        == "openai_compatible"
    ):
        provider_items.append(
            {
                "id": "openai_compatible",
                "label": "OpenAI-compatible provider",
                "base_url": settings.get("openai_compatible_url"),
                "input_cost_per_million": settings.get("model_input_cost_per_million"),
                "output_cost_per_million": settings.get("model_output_cost_per_million"),
                "currency": settings.get("model_cost_currency"),
                "api_key_env": settings.get("openai_api_key_env"),
                "enabled": True,
            }
        )
    for item in provider_items:
        if not isinstance(item, Mapping) or not item.get("id"):
            continue
        settings_id = str(item["id"]).strip().casefold()
        safe_id = safe_settings_identifier(settings_id)
        config_digest = _configuration_sha256(
            {
                "id": settings_id,
                "provider_type": str(item.get("provider_type") or "openai_compatible"),
                "base_url": str(item.get("base_url") or "").rstrip("/"),
                "selected_model": str(item.get("selected_model") or ""),
                "model_kind": str(item.get("model_kind") or "unknown"),
                "language_pair": str(item.get("language_pair") or ""),
                "supports_tools": bool(item.get("supports_tools", False)),
                "tool_attestation": dict(item.get("tool_attestation") or {}),
                "api_key_env": str(item.get("api_key_env") or ""),
            }
        )
        manifests.append(
            parse_extension_manifest(
                {
                    "schema_version": 1,
                    "id": f"provider.{safe_id}",
                    "name": str(item.get("label") or settings_id)[:80],
                    "version": "settings-v1",
                    "description": "Imported model API configured in Workbench settings.",
                    "publisher": "Local configuration",
                    "origin": "local",
                    "kind": "model_provider",
                    "category": "models",
                    "entrypoint": {
                        "type": "provider_settings",
                        "adapter": "model_provider",
                        "settings_id": settings_id,
                        "configuration_sha256": config_digest,
                    },
                    "permissions": [
                        _permission(
                            "network.model_provider",
                            "external_write",
                            "Send prompts to the configured provider.",
                        ),
                    ],
                    "health_probe": "model_provider",
                    "removable": True,
                    "default_installed": True,
                    "default_enabled": False,
                }
            )
        )
    return manifests


def enabled_settings_extension_ids(settings: Mapping[str, Any]) -> set[str]:
    """Identify pre-platform active settings for one-time compatible import."""

    result: set[str] = set()
    for item in settings.get("mcp_servers") or []:
        if isinstance(item, Mapping) and item.get("id") and item.get("enabled") is True:
            result.add(f"mcp.{safe_settings_identifier(item['id'])}")
    for item in settings.get("model_providers") or []:
        if isinstance(item, Mapping) and item.get("id") and item.get("enabled") is True:
            result.add(f"provider.{safe_settings_identifier(str(item['id']).casefold())}")
    if (
        not settings.get("model_providers")
        and str(settings.get("model_provider") or "ollama").casefold()
        == "openai_compatible"
    ):
        result.add("provider.openai_compatible")
    return result
