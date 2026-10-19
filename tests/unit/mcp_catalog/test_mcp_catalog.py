"""Tests for the MCP catalog module (isolated from community code).

Three layers, mirroring where the pilot bit us:

* ``openapi_import``  — conversion rules (verb filter, snake_case names,
  fail-closed empty roles, Go-template bodies);
* ``repo``            — schema shape via the real migration 021 on sqlite;
* ``publisher``       — the three gateway merge rules (ingress per service,
  plugin tools at rule top level, ext-auth single-matchRule invariant).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.mcp_catalog.models import McpServiceRow
from octop.mcp_catalog.nacos_client import NacosConfigError
from octop.mcp_catalog.openapi_import import import_openapi
from octop.mcp_catalog.gateway_client import GatewayClientError
from octop.mcp_catalog.publisher import (
    CatalogPublisher,
    build_plugin_tool,
    collect_acl_map,
)
from octop.mcp_catalog.repo import CatalogRepo


@pytest.fixture(autouse=True)
def _nacos_credentials_env(monkeypatch: pytest.MonkeyPatch):
    """router._nacos_creds is fail-closed (review 2026-10-02 P1-2): publish
    endpoints refuse to run without MCP_CATALOG_NACOS_PASSWORD. Unit tests
    never talk to a real nacos (clients are monkeypatched), so provide a
    module-wide dummy value."""
    monkeypatch.setenv("MCP_CATALOG_NACOS_PASSWORD", "unit-test-password")


# ---------------------------------------------------------------- openapi_import

_OPENAPI = {
    "openapi": "3.0.1",
    "paths": {
        "/rpc-api/cmc/bank-info/banks": {
            "post": {
                "operationId": "listBanks",
                "summary": "查询银行列表",
                "requestBody": {"content": {"application/json": {"schema": {}}}},
            }
        },
        "/rpc-api/cmc/agent/{agentId}": {
            "get": {
                "operationId": "get_agent_detail",
                "parameters": [
                    {
                        "name": "agentId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
            },
            "delete": {"operationId": "deleteAgent"},
            "patch": {"operationId": "patchAgent"},  # unsupported verb → skipped
        },
        "/rpc-api/cmc/dealer-info/update": {
            "put": {
                "operationId": "updateDealer",
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "required": ["dealerName"],
                    "properties": {"dealerName": {"type": "string"}},
                }}}},
            }
        },
        "/rpc-api/cmc/save": {
            "post": {
                "summary": "保存",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["agentName"],
                                "properties": {
                                    "agentName": {"type": "string"},
                                    "remark": {"type": "string"},
                                },
                            }
                        }
                    }
                },
            }
        },
    },
}


def test_import_converts_supported_verbs_only():
    report = import_openapi(_OPENAPI)
    names = {t["tool_name"] for t in report["tools"]}
    assert names == {
        "list_banks",
        "get_agent_detail",
        "post_rpc_api_cmc_save",
        "delete_agent",
        "update_dealer",
    }


def test_import_report_lists_skipped_instead_of_silently_dropping():
    """评审 2.4 回归：不可转换的操作必须出现在 skipped 报告里，不能静默丢弃。"""
    report = import_openapi(_OPENAPI)
    skipped = {(s["path"], s["method"], s["reason"]) for s in report["skipped"]}
    assert ("/rpc-api/cmc/agent/{agentId}", "PATCH", "unsupported_verb") in skipped
    assert len(report["tools"]) + len(report["skipped"]) == 6  # 6 operations in spec


def test_import_put_has_body_and_delete_has_none():
    """PUT 带请求体模板（与 POST 同路）；DELETE 无 body、仅路径参数。"""
    put_tool = next(
        t for t in import_openapi(_OPENAPI)["tools"] if t["tool_name"] == "update_dealer"
    )
    assert put_tool["http_method"] == "PUT"
    assert put_tool["request_body"] == '{ "dealerName":"{{.args.dealerName}}"}'
    del_tool = next(
        t for t in import_openapi(_OPENAPI)["tools"] if t["tool_name"] == "delete_agent"
    )
    assert del_tool["http_method"] == "DELETE"
    assert del_tool["request_body"] is None


def test_import_path_params_become_go_template():
    """评审 2.5 回归：路径参数必须转成 {{.name}} 模板，否则插件请求字面量 URL。"""
    tool = next(
        t for t in import_openapi(_OPENAPI)["tools"] if t["tool_name"] == "get_agent_detail"
    )
    assert tool["http_path"] == "/rpc-api/cmc/agent/{{.agentId}}"
    assert tool["parameters"][0]["location"] == "path"


def test_import_get_has_no_body_and_path_arg():
    tool = next(
        t for t in import_openapi(_OPENAPI)["tools"] if t["tool_name"] == "get_agent_detail"
    )
    assert tool["http_method"] == "GET"
    assert tool["request_body"] is None
    assert tool["parameters"][0]["name"] == "agentId"
    assert tool["parameters"][0]["location"] == "path"


def test_import_post_body_becomes_go_template_of_required_props():
    tool = next(
        t for t in import_openapi(_OPENAPI)["tools"] if t["tool_name"] == "post_rpc_api_cmc_save"
    )
    # required 属性无条件进模板；可选 remark 现在带条件渲染（2.5 后半）
    # 2026-10-06 起 string 槽位仍带引号，JSON 槽位（对象/数组/数字）不带引号
    assert tool["request_body"] == (
        '{ "agentName":"{{.args.agentName}}"'
        '{{if .args.remark}},"remark":"{{.args.remark}}"{{end}}}'
    )
    arg_names = {a["name"] for a in tool["parameters"]}
    assert arg_names == {"agentName", "remark"}


def test_import_optional_body_scalar_gets_conditional_template():
    """评审 2.5 后半回归：可选标量字段必须进模板（条件渲染），否则被插件静默丢弃。"""
    spec = {
        "openapi": "3.0.1",
        "paths": {
            "/things": {
                "post": {
                    "operationId": "createThing",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["name"],
                                    "properties": {
                                        "name": {"type": "string"},
                                        "remark": {"type": "string"},
                                        "count": {"type": "integer"},
                                    },
                                }
                            }
                        }
                    },
                }
            }
        },
    }
    tool = import_openapi(spec)["tools"][0]
    assert tool["request_body"] == (
        '{ "name":"{{.args.name}}"'
        '{{if .args.remark}},"remark":"{{.args.remark}}"{{end}}'
        '{{if .args.count}},"count":{{.args.count}}{{end}}}'
    )
    # 未传可选字段时模板渲染结果是合法 JSON 的骨架（无悬挂逗号）
    assert '{{if .args.remark}},"remark"' in tool["request_body"]


def test_import_resolves_request_body_ref():
    """springdoc $ref body must be inlined — 2026-10-01 CMC regression.

    Real doc: /rpc-api/cmc/bank-info/banks requestBody was
    {"$ref": "#/components/schemas/BankInfoReqDTO"}; the importer produced no
    body template and no body args, the LLM called with {} and CMC answered
    400 (request|body缺失).
    """
    spec = {
        "openapi": "3.1.0",
        "paths": {
            "/rpc-api/cmc/bank-info/banks": {
                "post": {
                    "operationId": "listBanks",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/BankInfoReqDTO"}
                            }
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "BankInfoReqDTO": {
                    "type": "object",
                    "properties": {
                        "country": {"type": "string", "description": "国家代码"},
                        "type": {"type": "string", "description": "银行分类"},
                    },
                }
            }
        },
    }
    tool = import_openapi(spec)["tools"][0]
    arg_names = {a["name"]: a for a in tool["parameters"]}
    assert set(arg_names) == {"country", "type"}
    assert all(a["location"] == "body" for a in arg_names.values())
    body = tool["request_body"]
    assert body is not None
    assert '"country":"{{.args.country}}"' in body
    assert '{{if .args.type}}' in body and '"type":"{{.args.type}}"' in body


def test_import_ref_with_required_and_nested_ref_props():
    """Refs inside properties resolve too; required props render unconditionally."""
    spec = {
        "openapi": "3.1.0",
        "paths": {
            "/orders": {
                "post": {
                    "operationId": "createOrder",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/OrderReq"}
                            }
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "OrderReq": {
                    "type": "object",
                    "required": ["dealerName"],
                    "properties": {
                        "dealerName": {"type": "string"},
                        "buyer": {"$ref": "#/components/schemas/Buyer"},
                    },
                },
                "Buyer": {
                    "type": "object",
                    "properties": {"buyerName": {"type": "string"}},
                },
            }
        },
    }
    tool = import_openapi(spec)["tools"][0]
    arg_names = {a["name"]: a for a in tool["parameters"]}
    assert set(arg_names) == {"dealerName", "buyer"}
    assert arg_names["dealerName"]["required"] is True
    body = tool["request_body"]
    assert body is not None
    assert body.startswith('{ "dealerName":"{{.args.dealerName}}"')
    # nested ref property resolves to an object — since 2026-10-06 it renders
    # as an unquoted conditional JSON slot (previously silently dropped from
    # the body, which made the argument unreachable from callers)
    assert '{{if .args.buyer}},"buyer":{{.args.buyer}}{{end}}' in body


def test_import_oneof_anyof_union_props_merge():
    """Review 2026-10-02 P1-4: springdoc emits anyOf/oneOf for union DTOs;
    an unhandled union degraded exactly like the unhandled $ref did (no body
    template → LLM calls upstream with empty args). The union of member
    properties must surface as arguments."""
    spec = {
        "openapi": "3.1.0",
        "paths": {
            "/notify": {
                "post": {
                    "operationId": "sendNotify",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/NotifyReq"}
                            }
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "NotifyReq": {
                    "anyOf": [
                        {"$ref": "#/components/schemas/MailReq"},
                        {"$ref": "#/components/schemas/SmsReq"},
                    ]
                },
                "MailReq": {
                    "type": "object",
                    "properties": {"mail_to": {"type": "string"}},
                },
                "SmsReq": {
                    "type": "object",
                    "properties": {"phone": {"type": "string"}},
                },
            }
        },
    }
    tool = import_openapi(spec)["tools"][0]
    arg_names = {a["name"] for a in tool["parameters"]}
    # union: members are alternatives — both sides' props appear, none forced
    assert {"mail_to", "phone"} <= arg_names
    for a in tool["parameters"]:
        assert a["required"] is False or a["name"] in ("mail_to", "phone")


def test_import_self_referential_ref_terminates():
    """Review 2026-10-02 P1-4: a recursive DTO (node.parent → itself, or
    A→B→A mutual) must not blow the stack and must not fabricate a template
    from the cycle — the depth guard degrades the cyclic property to an
    unconverted leaf while the plain fields keep working."""
    spec = {
        "openapi": "3.1.0",
        "paths": {
            "/nodes": {
                "post": {
                    "operationId": "createNode",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/NodeReq"}
                            }
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "NodeReq": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "parent": {"$ref": "#/components/schemas/NodeReq"},
                    },
                },
            }
        },
    }
    tool = import_openapi(spec)["tools"][0]  # must not RecursionError
    arg_names = {a["name"] for a in tool["parameters"]}
    assert "name" in arg_names


def test_import_unresolvable_ref_stays_unconverted():
    """An unknown $ref must not fabricate a body — old fail-safe behaviour."""
    spec = {
        "openapi": "3.1.0",
        "paths": {
            "/x": {
                "post": {
                    "operationId": "doX",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Missing"}
                            }
                        }
                    },
                }
            }
        },
        "components": {"schemas": {}},
    }
    tool = import_openapi(spec)["tools"][0]
    assert tool["request_body"] is None
    assert tool["parameters"] == []


def test_delegation_token_carries_issuer():
    """评审 3.11 配套：签发侧带 iss=octop，hub 可选 requireIssuer。"""
    import jwt as pyjwt

    from octop.infra.connectors.delegation import DELEGATION_ISSUER, mint_delegated_token

    token = mint_delegated_token(b"k" * 32, uid=1, org_id="o", role_keys=["r"], session_id="s")
    claims = pyjwt.decode(token, b"k" * 32, algorithms=["HS256"], audience="mcp-hub")
    assert claims["iss"] == DELEGATION_ISSUER == "octop"


def test_import_empty_body_keeps_literal_empty_object():
    tool = next(t for t in import_openapi(_OPENAPI)["tools"] if t["tool_name"] == "list_banks")
    assert tool["request_body"] == "{}"


def test_import_rejects_non_openapi():
    with pytest.raises(ValueError):
        import_openapi({"swagger": "2.0"})


# ---------------------------------------------------------------- repo


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture
def repo(db: SqlitePool) -> CatalogRepo:
    return CatalogRepo(db)


def _svc_fields(service_id="summer-cmc", **over):
    fields = {
        "service_id": service_id,
        "display_name": "渠管中心",
        "nacos_namespace": "local",
        "nacos_group": "DEFAULT_GROUP",
        "nacos_service_name": "summer-cmc",
        "backend_port": 48088,
    }
    fields.update(over)
    return fields


def _tool_fields(service_id="summer-cmc", **over):
    fields = {
        "service_id": service_id,
        "tool_name": "list_cmc_banks",
        "description": "查询银行列表",
        "http_method": "POST",
        "http_path": "/rpc-api/cmc/bank-info/banks",
        "request_body": "{}",
        "allowed_roles": ["cheryfs"],
    }
    fields.update(over)
    return fields


def test_service_roundtrip_and_destination(repo: CatalogRepo):
    row = repo.create_service(_svc_fields())
    assert row.resolved_mcp_path == "/api/internal/mcp/summer-cmc"
    assert row.ingress_name == "summer-cmc-mcp"
    # group underscores → hyphens, case preserved (pilot-verified format)
    assert row.nacos_destination() == "summer-cmc.DEFAULT-GROUP.local.nacos:48088"
    assert repo.get_service("summer-cmc") is not None
    assert repo.get_service("nope") is None


def test_tool_upsert_preserves_unique_name(repo: CatalogRepo):
    repo.create_service(_svc_fields())
    repo.upsert_tool(_tool_fields())
    repo.upsert_tool(_tool_fields(description="更新后的描述"))
    tools = repo.list_tools("summer-cmc")
    assert len(tools) == 1
    assert tools[0].description == "更新后的描述"
    assert tools[0].allowed_roles == ["cheryfs"]


def test_service_delete_cascades_tools(repo: CatalogRepo):
    repo.create_service(_svc_fields())
    repo.upsert_tool(_tool_fields())
    repo.delete_service("summer-cmc")
    assert repo.list_tools("summer-cmc") == []


def test_delete_tools_batch(repo: CatalogRepo):
    repo.create_service(_svc_fields())
    for name in ("tool_a", "tool_b", "tool_c"):
        repo.upsert_tool(_tool_fields(tool_name=name))
    # 未知名跳过而不是报错——并发编辑后的过期选择不能炸整批
    deleted = repo.delete_tools("summer-cmc", ["tool_b", "tool_c", "ghost_tool"])
    assert deleted == ["tool_b", "tool_c"]
    assert [t.tool_name for t in repo.list_tools("summer-cmc")] == ["tool_a"]


def test_delete_tools_scoped_to_service(repo: CatalogRepo):
    repo.create_service(_svc_fields("summer-cmc"))
    repo.create_service(_svc_fields("summer-laac"))
    repo.upsert_tool(_tool_fields(service_id="summer-cmc", tool_name="cmc_tool"))
    repo.upsert_tool(_tool_fields(service_id="summer-laac", tool_name="laac_tool"))
    assert repo.delete_tools("summer-cmc", ["laac_tool"]) == []
    assert repo.delete_tools("summer-cmc", ["cmc_tool"]) == ["cmc_tool"]
    # 其他服务的行不受影响
    assert repo.get_tool("summer-laac", "laac_tool") is not None


def test_publish_log_roundtrip(repo: CatalogRepo):
    repo.create_service(_svc_fields())
    repo.log_publish(
        "summer-cmc", "publish", "ok", {"acl": {"cheryfs": ["list_cmc_banks"]}}, actor="admin"
    )
    entries = repo.list_publish_log("summer-cmc")
    assert entries[0].result == "ok"
    assert "list_cmc_banks" in entries[0].detail


# ---------------------------------------------------------------- publisher


def _service_row(**over) -> McpServiceRow:
    base = {
        "service_id": "summer-cmc",
        "display_name": "渠管中心",
        "description": "",
        "nacos_namespace": "local",
        "nacos_group": "DEFAULT_GROUP",
        "nacos_service_name": "summer-cmc",
        "backend_port": 48088,
        "openapi_url": "",
        "mcp_path": None,
        "status": "draft",
        "created_at": 0,
        "updated_at": 0,
    }
    base.update(over)
    return McpServiceRow(**base)


def _tool_row(name="list_cmc_banks", roles=("cheryfs",), enabled=True, **over) -> object:
    from octop.mcp_catalog.models import McpToolRow

    fields = {
        "id": 1,
        "service_id": "summer-cmc",
        "tool_name": name,
        "description": "d",
        "http_method": "POST",
        "http_path": "/rpc-api/x",
        "parameters": [],
        "request_body": "{}",
        "response_body": None,
        "allowed_roles": list(roles),
        "enabled": enabled,
        "created_at": 0,
        "updated_at": 0,
    }
    fields.update(over)
    return McpToolRow(**fields)


def test_route_spec_shape_from_service_row():
    """v3（Console 通道）：路由形状 = RouteSpec.to_request() —— destination
    去 ':port' 即 Console 服务名（实测三型统一），路由名=mcp_path 前缀。"""
    from octop.mcp_catalog.gateway_client import RouteSpec, split_destination

    svc = _service_row()
    name, port = split_destination(svc.gateway_destination())
    spec = RouteSpec(
        name=svc.ingress_name, path=svc.resolved_mcp_path, service_name=name, service_port=port
    )
    body = spec.to_request()
    assert body["name"] == "summer-cmc-mcp"
    assert body["path"] == {"matchType": "PRE", "matchValue": "/api/internal/mcp/summer-cmc"}
    assert body["services"] == [
        {"name": "summer-cmc.DEFAULT-GROUP.local.nacos", "port": 48088, "weight": 100}
    ]


def test_plugin_tool_url_is_relative_and_top_level_tools():
    tool = build_plugin_tool(_tool_row())
    assert tool["requestTemplate"]["url"] == "/rpc-api/x"  # relative — route decides the cluster
    assert tool["requestTemplate"]["method"] == "POST"


def test_console_unknown_route_400_is_translated_to_not_found():
    """线上事故回归（2026-10-15）：Console 对**不存在路由**的实例端点返回
    400 ValidationException("Unknown route") 而非 404（console 2.2.4 实测）。
    服务下线（路由已删）后再上线的活体读取/摘除曾因此伪装成服务端故障抛
    502——必须译为 GatewayNotFound 语义让调用方的容忍逻辑生效。"""
    import httpx as _httpx

    from octop.mcp_catalog.gateway_client import GatewayNotFound, HigressConsoleClient

    def handler(request: _httpx.Request) -> _httpx.Response:
        if request.url.path == "/session/login":
            return _httpx.Response(201, json={"success": True})
        if "plugin-instances" in request.url.path:
            # 逐字复刻线上响应（用户报错原文）
            return _httpx.Response(
                400,
                json={
                    "success": False,
                    "message": "com.alibaba.higress.sdk.exception.ValidationException: "
                    f"Unknown route: {request.url.path.split('/')[3]}",
                    "data": None,
                },
            )
        return _httpx.Response(404, json={"success": False, "message": "not found"})

    client = HigressConsoleClient(
        "http://console.test", "admin", "pw", transport=_httpx.MockTransport(handler)
    )
    # 活体读取（publish_diff）：路由不存在 → None（published=False 全 added 分支）
    assert client.get_route_plugin("summer-laac-mcp", "mcp-server") is None
    # unpublish 收尾摘实例：路由不存在 → False（容忍，不算失败）
    assert client.delete_route_plugin("summer-laac-mcp", "mcp-server") is False
    # 语义分层：UnknownRoute 必须是 NotFound 子类（publisher 回滚按 NotFound 容忍）
    with pytest.raises(GatewayNotFound):
        client._request(
            "GET", "/v1/routes/nope/plugin-instances/mcp-server", allow_404=True
        )
    client.close()


def test_console_genuine_400_still_raises():
    """非 "Unknown route" 的 400 不能被吞成 None——真实校验拒绝仍是错误。"""
    import httpx as _httpx

    from octop.mcp_catalog.gateway_client import GatewayClientError, HigressConsoleClient

    def handler(request: _httpx.Request) -> _httpx.Response:
        if request.url.path == "/session/login":
            return _httpx.Response(201, json={"success": True})
        return _httpx.Response(
            400, json={"success": False, "message": "ValidationException: bad config"}
        )

    client = HigressConsoleClient(
        "http://console.test", "admin", "pw", transport=_httpx.MockTransport(handler)
    )
    with pytest.raises(GatewayClientError) as ei:
        client.get_route_plugin("summer-laac-mcp", "mcp-server")
    assert "Unknown route" not in str(ei.value)
    client.close()


def test_publish_writes_per_route_plugin_instances():
    """v3：每服务 = 一条路由 + 路由级插件实例（实例模型）。工具挂 mcp-server
    实例 configurations 顶层，ext-auth 实例 = 固化模板；同规则折叠细节归 Console。"""
    gw, acl, pub = _publisher_stub()
    svc = _service_row()
    detail = pub.publish(svc, [_tool_row()], {"cheryfs": ["t1"]})
    assert detail["written"] is True
    # mcp-server 实例：tools 在顶层，server.name=service_id
    mcp = gw.plugins[("summer-cmc-mcp", "mcp-server")]
    assert mcp["server"] == {"name": "summer-cmc", "type": "rest"}
    assert [t["name"] for t in mcp["tools"]] == ["list_cmc_banks"]
    # ext-auth 实例 = 固化线上模板（fail-closed by construction）
    assert gw.plugins[("summer-cmc-mcp", "ext-auth")]["failure_mode_allow"] is False
    # 路由先建（Console 拒绝对不存在路由挂实例，实测 400 Unknown route）
    assert "summer-cmc-mcp" in gw.routes
    assert detail["route"]["action"] == "created"


def test_publish_first_write_rolls_back_created_route_on_failure():
    """补偿回滚回归：首发（路由为本调用新建）时实例/ACL 写入失败 → 回滚删除
    路由，失败终态保持"无路由"（coverage-first 不变量的 Console 载体）。"""
    class _BoomGateway(_FakeGateway):
        def put_route_plugin(self, route, plugin, configurations):
            if plugin == "ext-auth":
                raise GatewayClientError("console 500")
            super().put_route_plugin(route, plugin, configurations)

    gw, acl = _BoomGateway(), _AclStub()
    pub = CatalogPublisher(gw, acl)
    with pytest.raises(GatewayClientError):
        pub.publish(_service_row(), [_tool_row()], {"cheryfs": ["t1"]})
    assert "summer-cmc-mcp" not in gw.routes
    assert gw.plugins == {}


def test_publish_updated_route_failure_keeps_route():
    """更新路径（路由已存在）失败 → 不回滚别人的路由，mixed 态交给 reconcile。"""
    class _BoomGateway(_FakeGateway):
        def put_route_plugin(self, route, plugin, configurations):
            if plugin == "ext-auth":
                raise GatewayClientError("console 500")
            super().put_route_plugin(route, plugin, configurations)

    gw, acl = _BoomGateway(), _AclStub()
    pub = CatalogPublisher(gw, acl)
    gw.routes["summer-cmc-mcp"] = {"name": "summer-cmc-mcp"}  # 已存在
    with pytest.raises(GatewayClientError):
        pub.publish(_service_row(), [_tool_row()], {"cheryfs": ["t1"]})
    assert "summer-cmc-mcp" in gw.routes  # 未回滚


def test_publish_drops_mcp_server_instance_when_no_enabled_tools():
    """v3：无启用工具 → 清掉残留 mcp-server 实例（旧数据面=移除该服务 rule）。"""
    gw, acl, pub = _publisher_stub()
    svc = _service_row()
    gw.plugins[("summer-cmc-mcp", "mcp-server")] = {"tools": [{"name": "stale"}]}
    detail = pub.publish(svc, [_tool_row(enabled=False)], {"cheryfs": []})
    assert detail["plugins"]["mcp-server"] == "cleared"
    assert ("summer-cmc-mcp", "mcp-server") not in gw.plugins


def test_acl_map_and_disabled_tools():
    acl = collect_acl_map(
        [
            _tool_row(name="t1", roles=("cheryfs", "guest")),
            _tool_row(name="t2", roles=("*",)),
            _tool_row(name="t3", roles=("cheryfs",), enabled=False),
        ],
        # 2026-10-01 subset rule: with NO service default there is nothing to
        # intersect with — explicit tool roles stay fail-closed unreachable.
        service_default_roles=["cheryfs", "guest", "*"],
    )
    assert acl == {"*": ["t2"], "cheryfs": ["t1"], "guest": ["t1"]}


# --------------------------------- 2026-10-01: 工具角色 ⊆ 服务角色（子集规则）


def test_acl_map_inherits_service_default_when_tool_empty():
    """工具未配置 → 继承服务默认；显式配置与服务默认取交集（只收窄不放大）。"""
    tools = [
        _tool_row(name="t_inherit", roles=()),
        _tool_row(name="t_narrow", roles=("expert_user",)),
        _tool_row(name="t_outside", roles=("guest",)),
        _tool_row(name="t_star", roles=("*",)),
    ]
    acl = collect_acl_map(tools, service_default_roles=["expert_user"])
    assert acl == {
        # explicit ⊆ default keeps the tool; outside roles and a bare ["*"]
        # widen past the default → intersected away (t_outside/t_star drop out)
        "expert_user": ["t_inherit", "t_narrow"],
    }


def test_acl_map_wildcard_service_default_lets_tool_config_stand():
    """服务默认含 '*' = 全集 → 工具显式配置原样生效。"""
    acl = collect_acl_map(
        [_tool_row(name="t1", roles=("vip",))],
        service_default_roles=["*"],
    )
    assert acl == {"vip": ["t1"]}


def test_effective_tool_roles_matrix():
    from octop.mcp_catalog.publisher import effective_tool_roles

    assert effective_tool_roles(None, ["a"]) == ["a"]  # empty → inherit
    assert effective_tool_roles([], ["a"]) == ["a"]
    assert effective_tool_roles(["a", "b"], ["a"]) == ["a"]  # narrowed
    assert effective_tool_roles(["a"], []) == []  # no default → fail closed
    assert effective_tool_roles(["x"], ["*"]) == ["x"]  # wildcard default


def test_validate_tool_roles_subset_rejects_outside_keys():
    from octop.mcp_catalog.publisher import validate_tool_roles_subset

    validate_tool_roles_subset(["a", "b"], ["b"])  # inside: fine
    validate_tool_roles_subset(["a"], [])  # empty config: fine
    validate_tool_roles_subset(["*"], ["anything"])  # wildcard default: fine
    with pytest.raises(ValueError, match="guest"):
        validate_tool_roles_subset(["a"], ["guest"])
    with pytest.raises(ValueError):
        validate_tool_roles_subset([], ["a"])  # no default at all


def test_prune_tools_to_service_roles(repo: CatalogRepo):
    """收窄服务默认角色时，显式工具配置被裁剪回子集（收窄意图是全局的）。"""
    repo.create_service(_svc_fields(service_id="svc-prune", allowed_roles=["r1", "r2"]))
    repo.upsert_tool(_tool_fields(service_id="svc-prune", tool_name="t_keep", allowed_roles=["r1"]))
    repo.upsert_tool(_tool_fields(service_id="svc-prune", tool_name="t_cut", allowed_roles=["r2"]))
    repo.upsert_tool(_tool_fields(service_id="svc-prune", tool_name="t_inherit", allowed_roles=[]))

    from octop.mcp_catalog.publisher import prune_tools_to_service_roles

    pruned = prune_tools_to_service_roles(repo, "svc-prune", ["r1"])
    assert pruned == ["t_cut"]
    tools = {t.tool_name: list(t.allowed_roles) for t in repo.list_tools("svc-prune")}
    assert tools == {"t_keep": ["r1"], "t_cut": [], "t_inherit": []}


def test_acl_map_no_service_default_stays_fail_closed():
    """服务默认也未配置 = 现状语义：空 ACL（不写条目 → 全员拒绝）。"""
    assert collect_acl_map([_tool_row(name="t1", roles=())]) == {}
    assert collect_acl_map([_tool_row(name="t1", roles=())], service_default_roles=None) == {}


def test_service_allowed_roles_roundtrip(repo: CatalogRepo):
    """033 迁移列 + repo 序列化：list 存取 roundtrip，缺省= []。"""
    repo.create_service(_svc_fields(service_id="svc-roles", allowed_roles=["expert_user"]))
    row = repo.get_service("svc-roles")
    assert row is not None
    assert row.allowed_roles == ["expert_user"]
    # PATCH 更新（list 序列化）
    repo.update_service("svc-roles", {"allowed_roles": ["r1", "r2"]})
    assert repo.get_service("svc-roles").allowed_roles == ["r1", "r2"]
    # 其他字段 PATCH 不动角色
    repo.update_service("svc-roles", {"display_name": "改名"})
    assert repo.get_service("svc-roles").allowed_roles == ["r1", "r2"]
    # 未传 allowed_roles 的创建 = []
    repo.create_service(_svc_fields(service_id="svc-plain"))
    assert repo.get_service("svc-plain").allowed_roles == []


def test_global_acl_inherits_service_default(repo: CatalogRepo):
    """033 router 侧 + 2026-10-01 子集规则：_global_acl 对未配置工具回退服务
    默认（单查询携出 svc_allowed_roles），显式工具配置只收窄不放大。"""
    from octop.mcp_catalog import router

    repo.create_service(_svc_fields(service_id="svc-a", allowed_roles=["expert_user"]))
    repo.create_service(_svc_fields(service_id="svc-b"))
    repo.update_service("svc-a", {"status": "published"})
    repo.update_service("svc-b", {"status": "published"})
    repo.upsert_tool({**_tool_fields(service_id="svc-a", tool_name="t_inherit",
                                     allowed_roles=[])})
    repo.upsert_tool({**_tool_fields(service_id="svc-a", tool_name="t_narrow",
                                     allowed_roles=["expert_user"])})
    repo.upsert_tool({**_tool_fields(service_id="svc-b", tool_name="t_plain",
                                     allowed_roles=[])})
    acl = router._global_acl(repo)
    # t_inherit 继承 svc-a 默认；t_narrow 与默认交集不变；越界的 ["*"] 工具
    # 配置被交集裁掉；svc-b 两层都空 → t_plain 不出现在任何角色（fail-closed）
    assert acl == {"expert_user": ["t_inherit", "t_narrow"]}


def test_global_acl_publish_override_uses_service_default(repo: CatalogRepo):
    """首次 publish 路径（override 分支）：draft 服务 + 服务默认 → ACL 含
    继承条目；unpublish 传 tools=[] 时本服务整体排除，行为不变。"""
    from octop.mcp_catalog import router

    repo.create_service(_svc_fields(allowed_roles=["expert_user"]))
    repo.upsert_tool({**_tool_fields(), "allowed_roles": []})
    svc = repo.get_service("summer-cmc")
    tools = repo.list_tools("summer-cmc")
    assert router._global_acl(repo, svc, tools) == {"expert_user": ["list_cmc_banks"]}
    assert router._global_acl(repo, svc, []) == {}


def test_service_org_id_roundtrip(repo: CatalogRepo):
    """034 三态归属：NULL=平台私有、'*'=平台共享、租户 id=硬边界；PATCH 可重归属。"""
    repo.create_service(_svc_fields(service_id="svc-priv"))
    repo.create_service(_svc_fields(service_id="svc-share", org_id="*"))
    repo.create_service(_svc_fields(service_id="svc-ten", org_id="acme"))
    assert repo.get_service("svc-priv").org_id is None
    assert repo.get_service("svc-share").org_id == "*"
    assert repo.get_service("svc-ten").org_id == "acme"
    repo.update_service("svc-priv", {"org_id": "acme"})
    assert repo.get_service("svc-priv").org_id == "acme"
    repo.update_service("svc-priv", {"org_id": None})
    assert repo.get_service("svc-priv").org_id is None
    # 其他字段 PATCH 不动归属
    repo.update_service("svc-ten", {"display_name": "改名"})
    assert repo.get_service("svc-ten").org_id == "acme"


def test_service_body_defaults_platform_share():
    """034: 创建 body 缺省归属 = 平台共享（'*'）——建服务时点不知道调用者，
    角色决策下放租户层；平台私有须显式传 null。"""
    from octop.mcp_catalog.router import ServiceBody

    body = ServiceBody(
        service_id="svc-x",
        display_name="n",
        nacos_service_name="svc",
        backend_port=8080,
    )
    assert body.org_id == "*"
    priv = ServiceBody(
        service_id="svc-y",
        display_name="n",
        nacos_service_name="svc",
        backend_port=8080,
        org_id=None,
    )
    assert priv.org_id is None


def test_service_body_accepts_null_allowed_roles():
    """前端表单清空默认角色发 null → 归一化为 []（与 description 同型）。"""
    from octop.mcp_catalog.router import ServiceBody

    body = ServiceBody(
        service_id="svc-null-roles",
        display_name="n",
        nacos_service_name="svc",
        backend_port=8080,
        allowed_roles=None,
    )
    assert body.allowed_roles == []


# --------------------------------------------- publisher（Console 对象模型）


class _FakeGateway:
    """Console 客户端假件：内存对象模型（路由 + 路由级插件实例）."""

    def __init__(self):
        self.routes: dict[str, dict] = {}
        self.plugins: dict[tuple[str, str], dict] = {}

    def get_route(self, name):
        return self.routes.get(name)

    def ensure_route(self, spec):
        action = "updated" if spec.name in self.routes else "created"
        self.routes[spec.name] = spec.to_request()
        return action

    def put_route_plugin(self, route, plugin, configurations):
        self.plugins[(route, plugin)] = configurations

    def get_route_plugin(self, route, plugin):
        return self.plugins.get((route, plugin))

    def delete_route(self, name):
        existed = name in self.routes
        self.routes.pop(name, None)
        for key in [k for k in self.plugins if k[0] == name]:
            del self.plugins[key]
        return existed

    def delete_route_plugin(self, route, plugin):
        return self.plugins.pop((route, plugin), None) is not None

    def list_service_sources(self):
        return list(_CONSOLE_SOURCES)


class _AclStub:
    """nacos 客户端假件：仅承载 mcp-hub ACL 分发通道."""

    def __init__(self):
        self.published: dict[str, str] = {}

    def publish_config(self, data_id, content, *, content_type="json"):
        self.published[data_id] = content
        return True

    def delete_config(self, data_id):
        return True


def _publisher_stub():
    """构造 (gateway, acl, CatalogPublisher) 三元组，测试按需检视前两者."""
    gw, acl = _FakeGateway(), _AclStub()
    return gw, acl, CatalogPublisher(gw, acl)


def test_publish_writes_acl_distribution_channel():
    gw, acl, pub = _publisher_stub()
    svc = _service_row()
    tools = [_tool_row(name="t1", roles=("cheryfs",))]
    global_acl = {"cheryfs": ["t1"], "guest": []}
    detail = pub.publish(svc, tools, global_acl)
    assert detail["written"] is True
    # ACL dataId 是分发通道：内容 = 调用方给的全局映射
    assert acl.published.get("mcphub.tool-acl.json") == json.dumps(
        {"cheryfs": ["t1"], "guest": []}, ensure_ascii=False, indent=2
    )


def test_publish_dry_run_does_not_write_acl():
    gw, acl, pub = _publisher_stub()
    svc = _service_row()
    tools = [_tool_row(name="t1", roles=("cheryfs",))]
    pub.publish(svc, tools, {"cheryfs": ["t1"]}, dry_run=True)
    assert acl.published == {}
    assert gw.routes == {} and gw.plugins == {}


# --------------------------------------------- review fixes (2026-09-25)


def test_unpublish_deletes_ingress_route():
    """评审 P0-1 回归：unpublish 必须真删路由（曾留裸 HTTP 代理直通业务系统）。
    v3：DELETE 路由先行（Console 级联清理实例规则），再显式摘两个实例。"""
    gw, acl, pub = _publisher_stub()
    gw.routes["summer-cmc-mcp"] = {"name": "summer-cmc-mcp"}
    detail = pub.unpublish(_service_row(status="published"), {"cheryfs": []})
    assert detail["route_delete"] == {"name": "summer-cmc-mcp", "deleted": True}
    assert "summer-cmc-mcp" not in gw.routes


def test_unpublish_missing_ingress_is_idempotent():
    gw, acl, pub = _publisher_stub()
    detail = pub.unpublish(_service_row(), None)
    assert detail["route_delete"]["deleted"] is False


def test_gateway_field_validation_blocks_yaml_injection():
    """评审 P0-3 回归：四个网关字段进 ingress YAML/destination，必须过字符白名单。"""
    from fastapi import HTTPException

    from octop.mcp_catalog.router import _validate_gateway_fields

    for field, bad in (
        ("nacos_service_name", "evil\n  higress.io/destination: http://attacker"),
        ("nacos_group", "A;B"),
        ("nacos_namespace", "local extra"),
        ("mcp_path", "no-leading-slash"),
        ("mcp_path", "/ok{inject}"),
    ):
        with pytest.raises(HTTPException) as ei:
            _validate_gateway_fields({field: bad})
        assert ei.value.status_code == 400, (field, bad)

    # 合法值不受影响
    _validate_gateway_fields(
        {
            "nacos_service_name": "summer-cmc",
            "nacos_group": "DEFAULT_GROUP",
            "nacos_namespace": "local",
            "mcp_path": "/api/internal/mcp/summer-cmc",
        }
    )


def test_openapi_url_scheme_whitelist_blocks_ssrf():
    """评审 P0-4 回归：urlopen 的 scheme 必须限定 http(s)。"""
    from fastapi import HTTPException

    from octop.mcp_catalog.router import _validate_openapi_url

    for bad in ("file:///app/secrets", "ftp://x/y", "localhost:8848/v3/api-docs", ""):
        with pytest.raises(HTTPException) as ei:
            _validate_openapi_url(bad)
        assert ei.value.status_code == 400
    _validate_openapi_url("http://127.0.0.1:48088/v3/api-docs")
    _validate_openapi_url("https://svc.local/v3/api-docs")


def test_patch_rejects_status_bypass():
    """评审 2.2 回归：PATCH 不得直改 status（否则未发布服务混进全局 ACL）。"""

    from octop.mcp_catalog.repo import CatalogRepo  # noqa: F401  (import sanity)
    from octop.mcp_catalog.router import ServicePatchBody

    body = ServicePatchBody(status="published")
    assert "status" in body.model_dump(exclude_none=True)
    # 端点层逻辑：fields 含 status → 400。直接断言端点源码里的守卫存在。
    import inspect

    from octop.mcp_catalog import router

    src = inspect.getsource(router.patch_service)
    assert "status is managed by the publish/unpublish endpoints" in src


def test_service_body_accepts_explicit_null_optional_fields():
    """v2 P1 回归：重构后的表单把清空的可选文本字段显式发成 null
    （openapi_url/description），pydantic 仅在字段缺省时套 "" 默认值，
    显式 null 会 422（DB 列为 NOT NULL DEFAULT ''）。"""
    from octop.mcp_catalog.router import ServiceBody

    body = ServiceBody(
        service_id="svc-null",
        display_name="n",
        nacos_service_name="svc",
        backend_port=8080,
        description=None,
        openapi_url=None,
        mcp_path=None,
    )
    assert body.description == ""
    assert body.openapi_url == ""
    assert body.mcp_path is None


def test_path_exact_conflict_rejected_and_nesting_allowed(repo: CatalogRepo):
    """第二轮评审 N2 回归：前缀嵌套合法（活体已证 Higress 最长前缀优先——
    summer-cmc 在 octop-pilot 的 /api/internal/mcp 下工作正常），
    仅精确同路径必须 409。旧的双向 startswith 校验会卡死真实拓扑。"""
    from fastapi import HTTPException

    from octop.mcp_catalog.router import _check_path_conflict

    repo.create_service(_svc_fields(service_id="octop-pilot", mcp_path="/api/internal/mcp"))
    repo.create_service(_svc_fields(service_id="summer-cmc"))  # /api/internal/mcp/summer-cmc
    # 嵌套与反向嵌套都放行（Pilot 兜底在前、子路径在后；反向同理）
    _check_path_conflict(repo, "summer-cmc2", "/api/internal/mcp/summer-cmc2")
    _check_path_conflict(repo, "new-parent", "/api/internal/mcp/summer-cmc/x")
    # 精确重复必须 409
    with pytest.raises(HTTPException) as ei:
        _check_path_conflict(repo, "impersonator", "/api/internal/mcp/summer-cmc")
    assert ei.value.status_code == 409


def test_publish_lock_serializes(repo: CatalogRepo):
    """评审 2.7 回归：发布锁互斥、可释放、TTL 过期后可接管。"""
    holder1 = repo.acquire_lock("mcp_publish:svc-a")
    with pytest.raises(RuntimeError):
        repo.acquire_lock("mcp_publish:svc-a")
    repo.release_lock("mcp_publish:svc-a", holder1)
    holder2 = repo.acquire_lock("mcp_publish:svc-a")
    assert holder2 != holder1
    repo.release_lock("mcp_publish:svc-a", holder1)
    # 旧持有者释放无效：holder2 的租约仍活着，第三次获取必须被拒
    with pytest.raises(RuntimeError):
        repo.acquire_lock("mcp_publish:svc-a")


def test_publish_lock_expires_and_is_taken_over(repo: CatalogRepo):
    holder1 = repo.acquire_lock("mcp_publish:svc-b", ttl_seconds=-1)  # 已过期租约
    assert holder1
    holder2 = repo.acquire_lock("mcp_publish:svc-b")
    assert holder2 != holder1
    repo.release_lock("mcp_publish:svc-b", holder2)


def test_upsert_tool_single_statement_conflict(repo: CatalogRepo):
    """评审 3.13 回归：ON CONFLICT 单语句 upsert 不再 SELECT-then-write。"""
    repo.create_service(_svc_fields())
    repo.upsert_tool(_tool_fields(enabled=True))
    repo.upsert_tool(_tool_fields(enabled=False, description="改"))
    tool = repo.get_tool("summer-cmc", "list_cmc_banks")
    assert tool is not None
    assert tool.enabled is False
    assert tool.description == "改"


# ------------------------------------------------ round-2 review regressions


def test_ext_auth_template_is_frozen_and_deep_copied():
    """ext-auth 配置 = 固化线上策略模板（fail-closed by construction）。
    实例模型下不再有跨服务 matchRules 合并（多规则猜测问题消亡），但模板
    深拷贝纪律必须保持：一次 publish 不得污染常量。"""
    import copy as _copy

    from octop.mcp_catalog.publisher import _EXT_AUTH_DEFAULT_RULE_CONFIG

    gw, acl, pub = _publisher_stub()
    before = _copy.deepcopy(_EXT_AUTH_DEFAULT_RULE_CONFIG)
    pub.publish(_service_row(), [_tool_row()], {"cheryfs": ["t1"]})
    written = gw.plugins[("summer-cmc-mcp", "ext-auth")]
    assert written == before
    assert written["failure_mode_allow"] is False
    assert written["status_on_error"] == 403
    # 2026-10-15 laac 事故回归：模板必须带 /mcp-api 白名单豁免——
    # mcp-server 插件的 REST 内部回环（无 Authorization）绝不能被 ext-auth 拦截
    assert written["match_type"] == "whitelist"
    assert written["match_list"] == [
        {"match_rule_path": "/mcp-api/", "match_rule_type": "prefix"}
    ]
    # 常量未被运行期写入污染
    assert _EXT_AUTH_DEFAULT_RULE_CONFIG == before


def test_ext_auth_hub_endpoint_default_keeps_frozen_template():
    """hub_endpoint 未配置 → ext-auth endpoint 与冻结模板逐字一致。"""
    from octop.mcp_catalog.publisher import _EXT_AUTH_DEFAULT_RULE_CONFIG

    gw, _acl, pub = _publisher_stub()
    pub.publish(_service_row(), [_tool_row()], {"cheryfs": ["t1"]})
    written = gw.plugins[("summer-cmc-mcp", "ext-auth")]
    assert (
        written["http_service"]["endpoint"]
        == _EXT_AUTH_DEFAULT_RULE_CONFIG["http_service"]["endpoint"]
    )
    assert written["http_service"]["endpoint"]["service_name"] == "mcp-hub.dns"
    assert written["http_service"]["endpoint"]["service_port"] == 8090


def test_ext_auth_hub_endpoint_injection_and_validation():
    """hub_endpoint 注入 endpoint（拓扑事实）；策略键仍冻结；非法格式 fail-loud。"""
    import pytest as _pytest

    from octop.mcp_catalog.publisher import (
        _EXT_AUTH_DEFAULT_RULE_CONFIG,
        ext_auth_rule_config,
    )

    gw, _acl, pub = _publisher_stub()
    pub.publish(
        _service_row(), [_tool_row()], {"cheryfs": ["t1"]},
        hub_endpoint="192.168.133.204:8090",
    )
    written = gw.plugins[("summer-cmc-mcp", "ext-auth")]
    assert written["http_service"]["endpoint"]["service_name"] == "192.168.133.204.dns"
    assert written["http_service"]["endpoint"]["service_port"] == 8090
    # 策略键不受 endpoint 注入影响
    assert written["failure_mode_allow"] is False
    assert written["status_on_error"] == 403
    assert written["match_type"] == "whitelist"
    # 冻结常量未被污染
    assert _EXT_AUTH_DEFAULT_RULE_CONFIG["http_service"]["endpoint"]["service_name"] == "mcp-hub.dns"

    # ".dns" 后缀幂等归一（带后缀配置等价裸 host）
    cfg = ext_auth_rule_config("mcp-hub.dns:8090")
    assert cfg["http_service"]["endpoint"]["service_name"] == "mcp-hub.dns"
    assert cfg["http_service"]["endpoint"]["service_port"] == 8090

    # None / 空 → 回冻结默认
    assert ext_auth_rule_config(None) == _EXT_AUTH_DEFAULT_RULE_CONFIG
    assert ext_auth_rule_config("") == _EXT_AUTH_DEFAULT_RULE_CONFIG

    # 非法格式 fail-loud（拒绝静默改写鉴权面）
    for bad in ("no-port", "host:", "host:0", "host:99999", "host:name"):
        with _pytest.raises(ValueError):
            ext_auth_rule_config(bad)


def test_import_prunes_only_openapi_source_tools(repo: CatalogRepo):
    """第二轮评审 N3 回归：删幽灵工具只能删 source='openapi'，
    手工工具（§8.3 正式途径）必须存活。"""
    repo.create_service(_svc_fields())
    repo.upsert_tool({**_tool_fields(tool_name="from_spec"), "source": "openapi"})
    repo.upsert_tool({**_tool_fields(tool_name="hand_defined"), "source": "manual"})
    deleted = repo.delete_imported_tools("summer-cmc", {"kept_openapi"})
    assert deleted == ["from_spec"]
    assert repo.get_tool("summer-cmc", "hand_defined") is not None


def test_nacos_client_treats_http_404_as_not_exist():
    """第二轮评审 M1 回归：缺失配置=HTTP 404（body code 20004 到不了 Python 层），
    get_config 必须 None / delete_config False，而不是抛异常。"""
    from octop.mcp_catalog.nacos_client import NacosConfigClient, NacosConfigError

    client = NacosConfigClient("http://127.0.0.1:1", "nacos", "nacos")

    def raise_404(req):
        raise NacosConfigError("nacos HTTP 404")

    client._request = raise_404  # type: ignore[method-assign]
    assert client.get_config("no.such.dataid") is None
    assert client.delete_config("no.such.dataid") is False


def test_http_path_validation_blocks_config_poisoning():
    """第二轮评审 §5 回归：畸形 http_path 会毁掉整份 mcp-server 插件配置。"""
    from fastapi import HTTPException

    from octop.mcp_catalog.router import _validate_http_path

    for bad in ("no-leading-slash", "/x/{y}", "/x/{{.y", "/x/{{}}", "/x/a b"):
        with pytest.raises(HTTPException) as ei:
            _validate_http_path(bad)
        assert ei.value.status_code == 400
    for ok in ("/rpc-api/cmc/banks", "/rpc-api/cmc/agent/{{.agentId}}", "/a/b_c-d.e"):
        _validate_http_path(ok)


def test_global_acl_single_query_matches_old_semantics(repo: CatalogRepo):
    """第二轮评审 §5 回归：list_enabled_tools_for_acl 单查询与逐服务查询等价。"""
    repo.create_service(_svc_fields(service_id="svc-a"))
    repo.create_service(_svc_fields(service_id="svc-b"))
    repo.update_service("svc-a", {"status": "published"})
    repo.update_service("svc-b", {"status": "published"})
    repo.upsert_tool({**_tool_fields(service_id="svc-a", tool_name="t1", allowed_roles=["r1"])})
    repo.upsert_tool(
        {**_tool_fields(service_id="svc-b", tool_name="t2", allowed_roles=["r1", "r2"])}
    )
    rows = repo.list_enabled_tools_for_acl()
    assert {sid for sid, _, _ in rows} == {"svc-a", "svc-b"}
    # exclude 语义：unpublish 路径排除单服务
    assert {sid for sid, _, _ in repo.list_enabled_tools_for_acl(exclude_service="svc-b")} == {
        "svc-a"
    }


# ------------------------------------------------ round-4 review regressions


def _render_body(template: str, values: dict[str, object]) -> str:
    """最小 Go text/template 渲染器：支持 {{.args.x}}、{{if .args.x}}…{{end}}、
    {{if or .args.x .args.y}}…{{end}}（内层优先消解 if 块，最后替换占位符）。
    插件契约（2026-09-25 活体证实）：工具参数挂在模板数据的 .args 路径下。"""
    import re as _re

    if_block = _re.compile(r"\{\{if ([^}]+)\}\}((?:(?!\{\{if ).)*?)\{\{end\}\}", _re.S)

    def _name(tok: str) -> str:
        return tok.strip().lstrip(".").removeprefix("args.")

    while "{{if" in template:

        def _repl(m: _re.Match[str]) -> str:
            cond = m.group(1).strip()
            if cond.startswith("or "):
                truthy = any(values.get(_name(n)) for n in cond[3:].split())
            else:
                truthy = bool(values.get(_name(cond)))
            return m.group(2) if truthy else ""

        template = if_block.sub(_repl, template)
    return _re.sub(
        r"\{\{\.args\.([A-Za-z0-9_]+)\}\}",
        lambda m: str(values.get(m.group(1), "")),
        template,
    )


def test_import_all_optional_body_renders_valid_json():
    """第四轮评审 N5 回归：0 必填 + ≥1 可选时，旧拼法产出 {,\"x\":…} 首逗号
    非法 JSON。新拼法：首个可选不带逗号、后续逗号用 or 守卫——四种传参
    组合渲染后都必须是合法 JSON。"""
    spec = {
        "openapi": "3.0.1",
        "paths": {
            "/notes": {
                "post": {
                    "operationId": "createNote",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "remark": {"type": "string"},
                                        "count": {"type": "integer"},
                                    },
                                }
                            }
                        }
                    },
                }
            }
        },
    }
    tool = import_openapi(spec)["tools"][0]
    template = tool["request_body"]
    for values, expected in (
        ({}, {}),
        ({"remark": "x"}, {"remark": "x"}),
        ({"count": 5}, {"count": 5}),
        ({"remark": "x", "count": 5}, {"remark": "x", "count": 5}),
    ):
        assert json.loads(_render_body(template, values)) == expected, values


def test_import_mixed_required_optional_body_still_renders_valid():
    """有必填字段时维持旧拼法（逗号在可选的 {{if}} 内），渲染仍合法。"""
    tool = next(
        t for t in import_openapi(_OPENAPI)["tools"] if t["tool_name"] == "post_rpc_api_cmc_save"
    )
    template = tool["request_body"]
    assert json.loads(_render_body(template, {"agentName": "a"})) == {"agentName": "a"}
    assert json.loads(_render_body(template, {"agentName": "a", "remark": "r"})) == {
        "agentName": "a",
        "remark": "r",
    }


def test_unpublish_last_service_then_publish_recovers():
    """第四轮评审 §3 死锁回归（实例模型下问题类消亡，保留自愈语义）：
    unpublish 摘掉最后一个服务后重新 publish 必须成功，且 ext-auth 实例
    按固化模板重建（fail-closed by construction）。"""
    gw, acl, pub = _publisher_stub()
    svc = _service_row(status="published")
    pub.unpublish(svc, {"cheryfs": []})
    # 摘除后无残留实例（旧代码在这一步之后 publish 永久 502）
    assert gw.plugins == {} and gw.routes == {}
    # 重新 publish：不再抛错，默认策略重建
    detail = pub.publish(svc, [_tool_row()], {"cheryfs": ["list_cmc_banks"]})
    ext = gw.plugins[("summer-cmc-mcp", "ext-auth")]
    assert ext["failure_mode_allow"] is False
    assert detail["plugins"]["ext-auth"] == "set"


def test_import_openapi_applies_gateway_validation(repo: CatalogRepo, monkeypatch):
    """第四轮评审 §6 回归：导入是外部输入源，必须过与 PUT /tools 相同的
    http_path / 参数校验——畸形 op 进 skipped（不静默丢弃），且坏工具的
    旧行被同步回收。"""
    import types as _types

    from octop.mcp_catalog import router

    repo.create_service(_svc_fields())
    repo.upsert_tool({**_tool_fields(tool_name="stale_bad"), "source": "openapi"})

    spec = {
        "openapi": "3.0.1",
        "paths": {
            "/a b/{id}": {  # 空格 → 畸形 http_path，必须被拒
                "get": {
                    "operationId": "badPath",
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                }
            },
            "/ok/{id}": {
                "get": {
                    "operationId": "okPath",
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                }
            },
        },
    }

    class _FakeResp:
        def __init__(self, payload):
            self._p = json.dumps(payload).encode()

        def read(self):
            return self._p

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=15: _FakeResp(spec))
    server = _types.SimpleNamespace(services=_types.SimpleNamespace(db=repo._db))
    result = router.import_openapi(
        "summer-cmc",
        router.ImportOpenApiBody(openapi_url="http://spec.local/v3/api-docs"),
        None,
        server,
    )
    assert result["imported"] == ["ok_path"]
    assert repo.get_tool("summer-cmc", "ok_path") is not None
    bad = [s for s in result["skipped"] if s["path"] == "/a b/{{.id}}"]
    assert bad and bad[0]["reason"].startswith("invalid:")
    assert repo.get_tool("summer-cmc", "bad_path") is None
    # 同步回收：上次导入的 stale_bad 本轮不在 spec 里（校验也拦不住删除语义）
    assert "stale_bad" in result["deleted"]
    assert repo.get_tool("summer-cmc", "stale_bad") is None


def test_publish_and_reconcile_compute_acl_inside_lock(repo: CatalogRepo, monkeypatch):
    """第四轮评审 §7 回归：_global_acl 必须在全局发布锁内计算（锁外算 =
    N1 竞态挪到 ACL 侧：慢发布者会用过期快照盖掉新 ACL）。
    判据：_global_acl 执行期间再次 acquire 全局锁必须 RuntimeError。"""
    import types as _types

    from octop.mcp_catalog import router

    repo.create_service(_svc_fields())
    repo.upsert_tool(_tool_fields())
    calls: list[bool] = []
    real_acl = router._global_acl

    def spy_acl(r, svc, tools=None):
        try:
            r.acquire_lock("mcp_publish:gateway")
            inside = False
        except RuntimeError:
            inside = True  # 锁被占 = 我们正处在锁内
        calls.append(inside)
        return real_acl(r, svc, tools)

    monkeypatch.setattr(router, "_global_acl", spy_acl)

    class _StubPub:
        def publish(self, svc, tools, acl_map, *, dry_run, fence=None, hub_endpoint=None):
            return {
                "acl": acl_map,
                "written": not dry_run,
                "route": {"name": "i", "path": "/", "service": {"name": "s", "port": 1}, "action": None},
                "plugins": {"ext-auth": "set", "mcp-server": "set"},
            }

    monkeypatch.setattr(router, "_publisher", lambda: _StubPub())
    server = _types.SimpleNamespace(services=_types.SimpleNamespace(db=repo._db))
    user = _types.SimpleNamespace(username="tester")

    router.publish_service("summer-cmc", router.PublishBody(), server, user)
    router.reconcile_service("summer-cmc", server, user)
    assert calls == [True, True]


def test_publish_blocks_roleless_platform_shared_service(repo: CatalogRepo, monkeypatch):
    """2026-10-01：平台共享服务空角色发布 = 死锁（租户无权补角色，永远无人
    可用）→ 发布入口 400 堵死；租户归属服务允许空角色（归属租户随后配置）。"""
    import types as _types

    from fastapi import HTTPException

    from octop.mcp_catalog import router

    repo.create_service(_svc_fields(service_id="svc-shared", org_id="*"))
    repo.upsert_tool(_tool_fields(service_id="svc-shared"))
    # Stub both channels: this test used to (accidentally) pass by
    # logging into whatever listens on localhost:8848 — on a dev box with a
    # live nacos3 it silently depended on real default credentials, and a
    # wrong password turned the dry-run branch into a 502 (review
    # 2026-10-02 P1-2 fallout). Hermetic now. v3: the gateway channel
    # (Console) is stubbed too — _publisher() builds both, and even a
    # dry-run publish reads the live route state through it.
    monkeypatch.setattr(router, "gateway_client_from_env", lambda: _FakeGateway())
    monkeypatch.setattr(router, "NacosConfigClient", lambda *a: _AclStub())
    server = _types.SimpleNamespace(services=_types.SimpleNamespace(db=repo._db))
    user = _types.SimpleNamespace(username="tester")

    with pytest.raises(HTTPException) as ei:
        router.publish_service("svc-shared", router.PublishBody(), server, user)
    assert ei.value.status_code == 400

    # 租户归属：空角色可发布（默认创建 body org_id='*'，这里显式覆盖）
    repo.create_service(
        _svc_fields(service_id="svc-tenant", org_id="acme", allowed_roles=[])
    )
    # distinct tool name: the hub ACL is keyed by bare tool_name across
    # services (review 2026-10-02 P1-3), so two services can no longer share one
    repo.upsert_tool(_tool_fields(service_id="svc-tenant", tool_name="list_tenant_things", allowed_roles=[]))
    # 没有 nacos 环境，publish 会在网关写入处失败——但 400 死锁校验在之前，
    # 能走到 502/连接错误即说明角色校验放行。用 dry_run 更干净：
    result = router.publish_service(
        "svc-tenant", router.PublishBody(dry_run=True), server, user
    )
    assert result["dry_run"] is True


# ------------------------------------------------- round-5 review regressions


def test_first_publish_acl_includes_own_tools(repo: CatalogRepo):
    """第五轮评审 §2 回归：首次 publish（status 仍为 draft）时 ACL 必须含
    本服务工具。旧代码 override 分支被 svc.status=="published" 挡住，而 DB
    查询又排除本服务 → 写出空 ACL；hub 对空对象按「已配置、全拒绝」处理
    （ToolAcl.replace），新发布服务对所有人 403 直到第二次 publish。"""
    from octop.mcp_catalog import router

    repo.create_service(_svc_fields(allowed_roles=["cheryfs"]))
    repo.upsert_tool(_tool_fields())
    svc = repo.get_service("summer-cmc")
    tools = repo.list_tools("summer-cmc")
    assert svc.status == "draft"  # publish 翻状态发生在 _global_acl 之后
    assert router._global_acl(repo, svc, tools) == {"cheryfs": ["list_cmc_banks"]}
    # unpublish 语义不变：tools=[] → 排除本服务后结果与旧实现一致
    assert router._global_acl(repo, svc, []) == {}


def test_validate_parameters_gates_only_template_names():
    """第五轮评审 §3 回归：标识符校验只对会进 Go 模板的名字（path 占位符、
    body 属性键）生效；query 参数原样拼进 query string，``page-size`` /
    ``filter.name`` / ``ids[]`` 必须放行——旧校验会让整个 op 被跳过并回收旧行。"""
    from fastapi import HTTPException

    from octop.mcp_catalog.router import _validate_parameters

    for ok in (
        {"name": "page-size", "location": "query"},
        {"name": "filter.name", "location": "query"},
        {"name": "ids[]", "location": "query"},
        {"name": "2faCode", "location": "query"},
        {"name": "id", "location": "path"},
        {"name": "agentName", "location": "body"},
    ):
        _validate_parameters([ok])
    for bad in (
        {"name": "page-size", "location": "path"},  # {{.page-size}} 非法模板
        {"name": "filter.name", "location": "body"},  # body 键进模板
        {"name": "", "location": "query"},
        {"name": "a b", "location": "query"},  # 空白会拆坏 query string
        {"name": "id", "location": "header"},
    ):
        with pytest.raises(HTTPException) as ei:
            _validate_parameters([bad])
        assert ei.value.status_code == 400, bad


def test_regex_anchors_reject_trailing_newline():
    """第六轮评审 §3 回归：Python ``$`` 匹配尾随换行前，``"abc\\n"`` 会被放行进
    ingress YAML（ScannerError→路由永远建不起来）。所有校验正则必须用 \\Z。"""

    from octop.mcp_catalog.router import (
        _HTTP_PATH_RE,
        _MCP_PATH_RE,
        _NACOS_NAME_RE,
        _PARAM_NAME_RE,
        _PARAM_QUERY_RE,
        _SERVICE_ID_RE,
        _TOOL_NAME_RE,
    )

    cases = [
        (_SERVICE_ID_RE, "summer-cmc"),
        (_TOOL_NAME_RE, "list_cmc_banks"),
        (_NACOS_NAME_RE, "summer-cmc"),
        (_MCP_PATH_RE, "/api/internal/mcp/summer-cmc"),
        (_HTTP_PATH_RE, "/rpc-api/cmc/banks"),
        (_PARAM_NAME_RE, "agentId"),
        (_PARAM_QUERY_RE, "page-size"),
    ]
    for pattern, value in cases:
        assert pattern.match(value) is not None, value
        assert pattern.match(value + "\n") is None, (pattern.pattern, value)


def test_go_template_body_gate():
    """第六轮评审 §2 回归：request_body/response_body 与 http_path 同引擎渲染，
    坏模板会毁掉整份插件配置——必须有闸。import 生成的模板（if/or/字段引用）
    按 by-construction 通过；模板变量（$x）、未知 token、块不配平、字面括号
    不配平全部拒绝。"""
    from fastapi import HTTPException

    from octop.mcp_catalog.router import _validate_go_template

    for ok in (
        '{"req":"{{.args.req}}"}',
        '{"a":{{.args.a}}{{if .b}},"b":"{{.args.b}}"{{end}}}',
        "{{if .x}}y{{else if .z}}n{{end}}",
        "{{/* 注释是合法 no-op（round-7 §3 误杀回归）*/}}{}",
        # round-8 §5 回归：括号子表达式是合法 Go（旧闸门误杀 `(eq`）
        "{{if and (eq .a 1) (ne .b 2)}}x{{end}}",
        "{{if (and (or .a .b) .c)}}x{{end}}",
        "plain json without templates",
    ):
        _validate_go_template(ok, "request_body")
    for bad in (
        '{"a": "{{.a}"',  # {{ 不配平
        "{{if .x}}unclosed",  # 块未闭合
        "{{end}}",  # 孤立 end
        "{{else}}",  # 块外 else
        "{{springfield .x}}",  # 未知函数
        "{{$v := .x}}{{$v}}",  # 模板变量赋值（未验证插件支持）
        '{"a": {{.args.x}}',  # 字面括号不配平（渲染后非合法 JSON）
        # round-7 §3 回归：块开头的条件同样被 Go 解析，必须进白名单
        "{{if @@@}}x{{end}}",
        "{{if (badfunc .x)}}y{{end}}",
        "{{range @@@}}x{{end}}",
        "{{with @@@}}y{{end}}",
        "{{if .a}}x{{else if @@@}}y{{end}}",
        "{{range $i, $v := .items}}x{{end}}",  # $var 未验证插件支持
        "{{with $x}}y{{end}}",
    ):
        with pytest.raises(HTTPException) as ei:
            _validate_go_template(bad, "request_body")
        assert ei.value.status_code == 400, bad
    # response_body 走同一道闸（字段名进报错）
    with pytest.raises(HTTPException) as ei:
        _validate_go_template("{{if .x}}", "response_body")
    assert "response_body" in ei.value.detail


def test_importer_body_roundtrip_through_gate():
    """第七轮评审 §2 回归：导入器亲手生成的 body 必须能过自己的闸门。
    旧回归手写期望串（开头补了 {"a":{{.args.a}}}）比真实产出宽容，绿灯漏过
    误杀——这里改为直接调 _build_body_template 再回灌，杜绝手写漂移。"""
    import json as _json

    from octop.mcp_catalog import openapi_import
    from octop.mcp_catalog.router import _validate_go_template

    # 6 种组合：0/1/2 必填 × 0/1/2 可选（可选标量走条件模板）
    # 2026-10-06 起签名改为 (required_specs, optional_specs)，pair 自带引号形态
    combos = [
        ([], [("remark", '"remark":"{{.args.remark}}"')]),
        (
            [],
            [("remark", '"remark":"{{.args.remark}}"'), ("count", '"count":{{.args.count}}')],
        ),
        (
            [],
            [("a", '"a":"{{.args.a}}"'), ("b", '"b":{{.args.b}}'), ("c", '"c":"{{.args.c}}"')],
        ),
        ([("req", '"req":"{{.args.req}}"')], [("opt", '"opt":"{{.args.opt}}"')]),
        (
            [("r1", '"r1":"{{.r1}}"'), ("r2", '"r2":"{{.r2}}"')],
            [("opt", '"opt":"{{.args.opt}}"')],
        ),
        ([], []),
    ]
    for required_specs, optional_specs in combos:
        template = openapi_import._build_body_template(required_specs, optional_specs)
        assert "{{{if" not in template, template  # 真正的 Go 解析器也过不了它
        _validate_go_template(template, "request_body")  # 不得再误杀自产 body
        # 渲染后的四种传参组合都必须是合法 JSON
        names = [p for p, _ in optional_specs]
        for provided in (
            {},
            {names[0]: "x"} if names else {},
            dict.fromkeys(names, 1),
        ):
            rendered = _render_body(
                template,
                {
                    **(required_specs and {"req": "r", "r1": "1", "r2": "2"} or {}),
                    **provided,
                },
            )
            _json.loads(rendered)


def test_go_template_gate_rejects_unclosed_comments():
    """P1-7 (review 2026-09-26): comment actions must be fully closed.

    {{/* x }} (no */) is a Go "unclosed comment" parse error and
    {{/* a */ badfunc .x}} fails Go's "comment ends before closing
    delimiter" — the old prefix-only check passed both, letting one bad
    body poison the whole plugin config.
    """
    from fastapi import HTTPException

    from octop.mcp_catalog.router import _validate_go_template

    # closed comment stays legal (round-7 §3 regression)
    _validate_go_template("{{/* ok */}}{}", "request_body")
    for bad in (
        "{{/*x}}",
        "{{/* unclosed }}",
        "{{/* a */ badfunc .x}}",
        '{"a":1}{{/*x}}',
    ):
        with pytest.raises(HTTPException) as ei:
            _validate_go_template(bad, "request_body")
        assert ei.value.status_code == 400, bad


def test_import_rejects_tool_names_outside_whitelist(repo: CatalogRepo, monkeypatch):
    """P1-8 (review 2026-09-26): imported tool names face the same whitelist
    as PUT /tools — a digit-leading operationId (2faVerify → 2fa_verify) or
    an over-long one must land in ``skipped``, never in the plugin config."""
    import types as _types

    from octop.mcp_catalog import router

    repo.create_service(_svc_fields())
    spec = {
        "openapi": "3.0.1",
        "paths": {
            "/2fa/verify": {"post": {"operationId": "2faVerify"}},  # digit-leading
            "/ok": {"get": {"operationId": "okThing"}},
        },
    }

    class _FakeResp:
        def __init__(self, payload):
            self._p = json.dumps(payload).encode()

        def read(self):
            return self._p

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=15: _FakeResp(spec))
    server = _types.SimpleNamespace(services=_types.SimpleNamespace(db=repo._db))
    result = router.import_openapi(
        "summer-cmc",
        router.ImportOpenApiBody(openapi_url="http://spec.local/v3/api-docs"),
        None,
        server,
    )
    assert result["imported"] == ["ok_thing"]
    bad = [s for s in result["skipped"] if s["path"] == "/2fa/verify"]
    assert bad and bad[0]["reason"].startswith("invalid:")
    assert bad[0]["reason"].startswith("invalid:tool_name")
    assert repo.get_tool("summer-cmc", "2fa_verify") is None


# --------------------------------------------- review 2026-09-26 (P1-9/P1-10)


def test_unpublish_rejects_non_published_service(repo: CatalogRepo):
    """P1-9: unpublishing a draft service must 409 — the old rollback path
    flipped failed unpublishes to status='published', pushing a never-published
    service (unreviewed roles, no gateway config) into the global ACL query."""
    from fastapi import HTTPException

    from octop.mcp_catalog import router

    repo.create_service(_svc_fields())
    body = router.PublishBody()
    server = _types_server(repo)
    with pytest.raises(HTTPException) as ei:
        router.unpublish_service("summer-cmc", body, server, _types_user())
    assert ei.value.status_code == 409
    svc = repo.get_service("summer-cmc")
    assert svc is not None
    assert svc.status == "draft"


def test_unpublish_failure_rolls_back_to_original_status(repo: CatalogRepo, monkeypatch):
    """P1-9 follow-up: for a *published* service a failed unpublish still
    rolls back to published (status before the flip)."""
    from octop.mcp_catalog import router

    repo.create_service(_svc_fields())
    repo.update_service("summer-cmc", {"status": "published"})

    from fastapi import HTTPException

    class _BoomPub:
        def unpublish(self, svc, remaining_acl, *, dry_run, fence=None):
            raise RuntimeError("nacos down")

    monkeypatch.setattr(router, "_publisher", lambda: _BoomPub())
    with pytest.raises(HTTPException) as ei:
        router.unpublish_service(
            "summer-cmc", router.PublishBody(), _types_server(repo), _types_user()
        )
    assert ei.value.status_code == 502
    svc = repo.get_service("summer-cmc")
    assert svc is not None
    assert svc.status == "published"


def _types_server(repo: CatalogRepo):
    import types as _types

    return _types.SimpleNamespace(services=_types.SimpleNamespace(db=repo._db))


def _types_user():
    import types as _types

    return _types.SimpleNamespace(username="tester")


def test_renew_lock_fences_lost_holder(repo: CatalogRepo):
    """P1-10: renew_lock renews only a live lease held by the caller."""
    from octop.mcp_catalog.repo import PUBLISH_LOCK_TTL_SECONDS

    holder = repo.acquire_lock("mcp_publish:gateway")
    assert repo.renew_lock("mcp_publish:gateway", holder) is True
    # a foreign holder can never renew someone else's lease
    assert repo.renew_lock("mcp_publish:gateway", "not-the-holder") is False
    # an expired lease is dead even for its original holder (someone may
    # already have taken over between expiry and the check)
    repo.acquire_lock("mcp_publish:fence", ttl_seconds=-1)
    assert repo.renew_lock("mcp_publish:fence", holder, ttl_seconds=PUBLISH_LOCK_TTL_SECONDS) is False


def test_publish_fence_aborts_after_lease_loss(repo: CatalogRepo, monkeypatch):
    """P1-10: once the lease is lost, the publisher stops writing nacos —
    the fence raises between writes instead of clobbering the successor's
    merged snapshot with our stale one."""
    import types as _types

    from octop.mcp_catalog import router
    from octop.mcp_catalog.publisher import CatalogPublisher

    repo.create_service(_svc_fields())
    repo.upsert_tool(_tool_fields())

    writes: list[str] = []

    class _RecordingGateway(_FakeGateway):
        def put_route_plugin(self, route, plugin, configurations):
            writes.append(f"put:{plugin}")
            super().put_route_plugin(route, plugin, configurations)

        def ensure_route(self, spec):
            writes.append(f"route:{spec.name}")
            return super().ensure_route(spec)

    class _RecordingAcl(_AclStub):
        def publish_config(self, data_id, content, *, content_type="json"):
            writes.append(f"acl:{data_id}")
            return super().publish_config(data_id, content, content_type=content_type)

    fence_calls = {"n": 0}

    def fence():
        fence_calls["n"] += 1
        if fence_calls["n"] > 1:
            raise RuntimeError("publish lock lost mid-flight")

    with pytest.raises(RuntimeError):
        CatalogPublisher(_RecordingGateway(), _RecordingAcl()).publish(
            repo.get_service("summer-cmc"),
            repo.list_tools("summer-cmc"),
            {"role": ["summer-cmc_tool"]},
            dry_run=False,
            fence=fence,
        )
    # first write happened, then the fence aborted the rest. Publish order
    # (Console constraint): route FIRST (plugin instances need an existing
    # route), then coverage via compensation — a fence abort on a freshly
    # created route rolls it back, so the terminal state stays "no route".
    assert writes == ["route:summer-cmc-mcp"]

    # router level: a fence failure surfaces as 502 with a publish log entry
    class _FenceStubPub:
        def publish(self, svc, tools, acl_map, *, dry_run, fence=None, hub_endpoint=None):
            fence()  # router fence -> renew_lock -> False
            return {
                "acl": acl_map,
                "written": not dry_run,
                "route": {"name": "i", "path": "/", "service": {"name": "s", "port": 1}, "action": None},
                "plugins": {"ext-auth": "set", "mcp-server": "set"},
            }

    monkeypatch.setattr(router, "_publisher", lambda: _FenceStubPub())
    monkeypatch.setattr(
        router.CatalogRepo, "renew_lock", lambda self, key, holder, **kw: False, raising=False
    )
    server = _types.SimpleNamespace(services=_types.SimpleNamespace(db=repo._db))
    user = _types.SimpleNamespace(username="tester")
    repo.update_service("summer-cmc", {"status": "draft"})  # first publish path
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        router.publish_service("summer-cmc", router.PublishBody(), server, user)
    assert ei.value.status_code == 502


# ---------------------------------------------------------------- sources (v3: 服务来源实时读 Console)

_CONSOLE_SOURCES = [
    {
        "name": "nacos",
        "type": "nacos2",
        "domain": "nacos3:8848",
        "port": 8848,
        "properties": {"nacosNamespaceId": "local", "nacosGroups": ["DEFAULT_GROUP"]},
    },
    {"name": "eureka", "type": "eureka", "domain": "eureka:8761", "port": 8761},
    {"name": "octop-pilot", "type": "static", "domain": "172.29.0.9:9099", "port": 80},
    {"no-name-entry": True},  # 缺 name/type 的条目跳过（controller 自身不可用的来源）
]


def test_parse_service_sources_extracts_live_shape():
    """活体 Console 形状（2026-10-15 实测）：下拉=name/type/domain/port 投影；
    nacos2 的 namespace/group 在 properties.nacosNamespaceId/nacosGroups。"""
    from octop.mcp_catalog.sources import parse_service_sources

    sources = parse_service_sources(_CONSOLE_SOURCES)
    assert [s["name"] for s in sources] == ["nacos", "eureka", "octop-pilot"]
    nacos = sources[0]
    assert nacos["type"] == "nacos2"
    assert nacos["domain"] == "nacos3:8848"
    assert nacos["port"] == 8848
    # nacos registry 额外透传默认 namespace（naming 查询补全用）
    assert nacos["nacos_namespace_id"] == "local"
    assert nacos["nacos_group"] == "DEFAULT_GROUP"
    assert "nacos_namespace_id" not in sources[1]  # eureka 无此字段
    assert sources[2] == {
        "name": "octop-pilot",
        "type": "static",
        "domain": "172.29.0.9:9099",
        "port": 80,
    }


def test_parse_service_sources_rejects_malformed_fail_closed():
    """空列表会伪装成「没有可用来源」误导管理员——畸形载荷必须显式报错。"""
    from octop.mcp_catalog.sources import parse_service_sources

    with pytest.raises(ValueError):
        parse_service_sources("not-a-list")
    with pytest.raises(ValueError):
        parse_service_sources({"data": _CONSOLE_SOURCES})  # 忘了解信封
    with pytest.raises(ValueError):
        parse_service_sources(None)


def test_list_service_sources_unreachable_is_error_not_empty(monkeypatch):
    """Console 不可达 → GatewayClientError（路由层 502），绝不静默返回空列表。"""
    from octop.mcp_catalog import sources as sources_mod

    class _DownClient:
        def list_service_sources(self):
            raise GatewayClientError("console: GET /v1/service-sources → HTTP 502")

    with pytest.raises(GatewayClientError):
        sources_mod.list_service_sources(_DownClient())


def test_list_sources_endpoint_fail_closed(monkeypatch):
    """路由层：Console 不可达/畸形载荷 → 502 带原因；正常 → sources 原样透传。"""
    from fastapi import HTTPException

    from octop.mcp_catalog import router
    from octop.mcp_catalog import sources as sources_mod

    monkeypatch.setattr(router, "gateway_client_from_env", lambda: None)  # 透传给 stub

    monkeypatch.setattr(
        sources_mod,
        "list_service_sources",
        lambda *a: (_ for _ in ()).throw(GatewayClientError("console down")),
    )
    with pytest.raises(HTTPException) as ei:
        router.list_sources(None)
    assert ei.value.status_code == 502
    assert "unavailable" in ei.value.detail

    monkeypatch.setattr(
        sources_mod, "list_service_sources", lambda *a: (_ for _ in ()).throw(ValueError("bad"))
    )
    with pytest.raises(HTTPException) as ei:
        router.list_sources(None)
    assert ei.value.status_code == 502
    assert "malformed" in ei.value.detail

    monkeypatch.setattr(
        sources_mod,
        "list_service_sources",
        lambda *a: [{"name": "nacos", "type": "nacos2", "domain": "nacos3:8848", "port": 8848}],
    )
    out = router.list_sources(None)
    assert out["sources"] == [
        {
            "name": "nacos",
            "type": "nacos2",
            "domain": "nacos3:8848",
            "port": 8848,
            "verified": True,  # P2.3: nacos2 属已验证类型
        }
    ]


# ------------------------------------------------- v2 P1.4: 下发配置预览端点


def test_preview_tool_renders_publish_shape(repo: CatalogRepo):
    """P1.4：preview 必须复用 build_plugin_tool（发布同一权威函数），产物即
    wasmplugins.mcp-server 的 tools[] 条目；不落库、不改 status。"""
    import types as _types

    from octop.mcp_catalog import router

    repo.create_service(_svc_fields())
    fields = _tool_fields()
    body = router.ToolBody(
        tool_name=fields["tool_name"],
        description=fields["description"],
        http_method=fields["http_method"],
        http_path=fields["http_path"],
        parameters=[{"name": "keyword", "location": "query", "required": True}],
        request_body=fields["request_body"],
        allowed_roles=fields["allowed_roles"],
    )
    server = _types.SimpleNamespace(services=_types.SimpleNamespace(db=repo._db))
    out = router.preview_tool("summer-cmc", body, None, server)

    entry = out["plugin_tool"]
    assert entry["name"] == "list_cmc_banks"
    assert entry["requestTemplate"]["method"] == "POST"
    assert entry["requestTemplate"]["url"] == "/rpc-api/cmc/bank-info/banks"
    assert entry["requestTemplate"]["body"] == "{}"
    assert entry["requestTemplate"]["headers"][0]["key"] == "Content-Type"
    # build_plugin_tool 的字典展开顺序会同时保留 location 与 position
    # （发布路径同形状，插件忽略未知键——docstring 已注明）
    assert entry["args"][0] == {
        "name": "keyword",
        "location": "query",
        "required": True,
        "position": "query",
    }
    assert "responseTemplate" not in entry  # 未填 response_body 不渲染该键
    # 不落库
    assert repo.get_tool("summer-cmc", "list_cmc_banks") is None


def test_preview_tool_validates_like_publish(repo: CatalogRepo):
    """坏 Go 模板在预览阶段就要报 400 带原因（不能等发布才炸掉整个插件配置）。"""
    import types as _types

    from fastapi import HTTPException

    from octop.mcp_catalog import router

    repo.create_service(_svc_fields())
    body = router.ToolBody(
        tool_name="bad_tool",
        http_method="GET",
        http_path="/api/x",
        response_body="{{.data | evilfunc}}",
    )
    server = _types.SimpleNamespace(services=_types.SimpleNamespace(db=repo._db))
    with pytest.raises(HTTPException) as ei:
        router.preview_tool("summer-cmc", body, None, server)
    assert ei.value.status_code == 400
    assert "unknown template token" in ei.value.detail


# ------------------------------------------------- v2 P2: registry 感知与门禁

_CONSOLE_SOURCES_P2 = [
    {"name": "nacos", "type": "nacos2", "domain": "nacos3:8848", "port": 8848},
    {"name": "eureka-src", "type": "eureka", "domain": "eureka:8761", "port": 8761},
    {"name": "pilot-static", "type": "static", "domain": "172.29.0.9:9099", "port": 80},
]


def _sources_gateway():
    class _G(_FakeGateway):
        def list_service_sources(self):
            return list(_CONSOLE_SOURCES_P2)

    return _G()


def test_gateway_destination_dispatch_by_registry_type():
    """P2.1：按 registry 类型分派。nacos2 默认路径逐字节兼容存量；
    static 格式以活体 ingress 为准（octop-pilot.static:8088 —— 端口取
    服务行 backend_port，不是 registry 条目 port）。"""
    svc = _service_row()
    assert svc.gateway_destination() == "summer-cmc.DEFAULT-GROUP.local.nacos:48088"
    assert svc.nacos_destination() == "summer-cmc.DEFAULT-GROUP.local.nacos:48088"
    assert svc.gateway_destination(
        registry_type="static", registry_name="pilot-static"
    ) == "pilot-static.static:48088"
    assert svc.gateway_destination(registry_type="dns", registry_name="d") == "d.dns:48088"
    # 未验证类型（eureka/zk/consul）合成推测格式，但发布被门禁拦（另测）
    assert svc.gateway_destination(
        registry_type="eureka", registry_name="eureka-src"
    ) == "summer-cmc.eureka-src:48088"
    # group 下划线 → 连字符
    assert _service_row(nacos_group="MY_GROUP").gateway_destination().startswith(
        "summer-cmc.MY-GROUP."
    )


def test_resolve_registry_found_and_missing():
    from octop.mcp_catalog.publisher import resolve_registry

    gw = _sources_gateway()
    entry = resolve_registry(gw, "eureka-src")
    assert entry["type"] == "eureka"
    with pytest.raises(ValueError, match="not found in live service sources"):
        resolve_registry(gw, "nope")

    class _BadPayload:
        def list_service_sources(self):
            return {"not": "a list"}

    with pytest.raises(ValueError):
        resolve_registry(_BadPayload(), "eureka-src")


def test_publish_refuses_unverified_registry_type():
    """P2.3 门禁：eureka destination 格式未探针验证 → publisher 拒发。"""
    svc = _service_row(registry_name="eureka-src")
    with pytest.raises(ValueError, match="not live-verified"):
        CatalogPublisher(_sources_gateway(), _AclStub()).publish(
            svc, [_tool_row()], {"cheryfs": ["t1"]}
        )


def test_publish_static_registry_uses_live_calibrated_destination():
    """static 已被活体 ingress 验证（octop-pilot.static:8088）→ 门禁放行，
    路由服务名按 static 格式合成（端口取服务行 backend_port）。"""
    svc = _service_row(registry_name="pilot-static")
    detail = CatalogPublisher(_sources_gateway(), _AclStub()).publish(
        svc, [_tool_row()], {"cheryfs": ["t1"]}
    )
    assert detail["route"]["service"] == {"name": "pilot-static.static", "port": 48088}


def test_default_registry_keeps_legacy_path_without_sources_lookup():
    """registry_name='nacos'（默认）不查 service-sources —— 存量服务与
    既有测试的 stub 网关（空来源列表）零漂移。"""
    detail = CatalogPublisher(_FakeGateway(), _AclStub()).publish(
        _service_row(), [_tool_row()], {"cheryfs": ["t1"]}
    )
    assert detail["route"]["service"] == {
        "name": "summer-cmc.DEFAULT-GROUP.local.nacos",
        "port": 48088,
    }


# ------------------------------------------------- v2 P3: naming 联动

class _NacosNamingClient:
    """admin_get stub with the LIVE-verified v3 admin ns shapes (2026-09-29)."""

    def __init__(self, services=None, instances=None, fail=False):
        self._services = services or []
        self._instances = instances or []
        self._fail = fail

    def admin_get(self, path, params):
        if self._fail:
            raise NacosConfigError("nacos request failed: refused")
        if path.endswith("/service/list"):
            return {
                "code": 0,
                "data": {
                    "pageItems": [{"name": n, "groupName": params.get("groupName")}
                                  for n in self._services]
                },
            }
        if path.endswith("/instance/list"):
            return {"code": 0, "data": self._instances}
        raise AssertionError(f"unexpected path {path}")


def test_list_nacos_services_parses_live_shape():
    from octop.mcp_catalog import naming

    client = _NacosNamingClient(services=["echo-demo", "summer-cmc"])
    assert naming.list_nacos_services(client, namespace="local", group="DEFAULT_GROUP") == [
        "echo-demo", "summer-cmc"
    ]


def test_nacos_instance_prefers_healthy_and_reports_shape():
    from octop.mcp_catalog import naming

    client = _NacosNamingClient(instances=[
        {"ip": "10.0.0.2", "port": 9099, "healthy": False},
        {"ip": "10.0.0.1", "port": 8080, "healthy": True},
    ])
    inst = naming.nacos_instance(client, namespace="local", group="DEFAULT_GROUP",
                                 service_name="echo-demo")
    assert inst == {"ip": "10.0.0.1", "port": 8080, "namespace": "local",
                    "group": "DEFAULT_GROUP"}


def test_nacos_instance_no_host_is_error():
    from octop.mcp_catalog import naming

    with pytest.raises(naming.NamingError, match="no instance registered"):
        naming.nacos_instance(_NacosNamingClient(instances=[]), namespace="local",
                              group="DEFAULT_GROUP", service_name="x")


def test_eureka_services_and_instance(monkeypatch):
    from octop.mcp_catalog import naming

    doc = {"applications": {"application": [
        {"name": "USER-SERVICE", "instance": [{"status": "UP", "hostName": "h1",
                                               "port": {"$": 8081}}]},
        {"name": "ORDER-SERVICE", "instance": [{"status": "STARTING", "hostName": "h2",
                                                "port": {"$": 8082}}]},
    ]}}
    def fake_get(domain, path):
        # 单应用端点返回 {"application": {...}}（eureka 真实形状），全量端点
        # 返回 {"applications": {...}}
        if path.startswith("/eureka/apps/"):
            name = path.rsplit("/", 1)[1]
            for a in doc["applications"]["application"]:
                if a["name"] == name:
                    return {"application": a}
            raise naming.NamingError(f"HTTP 404 for {name}")
        return doc

    monkeypatch.setattr(naming, "_eureka_get", fake_get)
    assert naming.list_eureka_services("eureka:8761") == ["ORDER-SERVICE", "USER-SERVICE"]
    inst = naming.eureka_instance("eureka:8761", "user-service")  # 小写入参 → 自动试大写
    assert inst == {"ip": "h1", "port": 8081, "namespace": None, "group": None}


class _ConsoleNamingStub:
    """list_services stub with the LIVE-verified /v1/services shape (2.2.4)."""

    def __init__(self, rows=None, fail=False):
        self._rows = rows or []
        self._fail = fail

    def list_services(self):
        if self._fail:
            from octop.mcp_catalog.gateway_client import GatewayClientError

            raise GatewayClientError("console down")
        return list(self._rows)


_CONSOLE_ROWS = [
    {"name": "summer-laac.DEFAULT-GROUP.local.nacos", "namespace": "mcp",
     "port": 48084, "endpoints": ["192.168.5.20"], "protocol": "HTTP"},
    {"name": "summer-cmc.OTHER.nsx.nacos", "namespace": "mcp",
     "port": 48088, "endpoints": ["10.0.0.9"], "protocol": "HTTP"},
    {"name": "mcp-hub.dns", "namespace": "mcp",
     "port": 8090, "endpoints": ["hub"], "protocol": "HTTP"},
]


def test_list_console_services_filters_by_group_and_namespace():
    from octop.mcp_catalog import naming

    client = _ConsoleNamingStub(rows=_CONSOLE_ROWS)
    assert naming.list_console_services(client, namespace="local",
                                        group="DEFAULT_GROUP") == ["summer-laac"]
    # 其他 group/namespace 不串
    assert naming.list_console_services(client, namespace="nsx",
                                        group="OTHER") == ["summer-cmc"]
    # dns/static 型不进 nacos 结果
    assert naming.list_console_services(client, namespace="mcp",
                                        group="x") == []


def test_console_instance_exact_and_prefix_fallback():
    from octop.mcp_catalog import naming

    client = _ConsoleNamingStub(rows=_CONSOLE_ROWS)
    inst = naming.console_instance(client, namespace="local",
                                   group="DEFAULT_GROUP", service_name="summer-laac")
    assert inst == {"ip": "192.168.5.20", "port": 48084,
                    "namespace": "local", "group": "DEFAULT_GROUP"}
    # 来源记录 group/namespace 漂移 → 前缀回退
    inst = naming.console_instance(client, namespace="prd", group="summer",
                                   service_name="summer-laac")
    assert inst == {"ip": "192.168.5.20", "port": 48084,
                    "namespace": "prd", "group": "summer"}


def test_console_instance_no_endpoint_is_error():
    from octop.mcp_catalog import naming

    with pytest.raises(naming.NamingError, match="no live endpoint"):
        naming.console_instance(
            _ConsoleNamingStub(rows=[
                {"name": "ghost.DEFAULT-GROUP.local.nacos", "port": 1,
                 "endpoints": []}]),
            namespace="local", group="DEFAULT_GROUP", service_name="ghost")


def test_console_failure_translates_to_naming_error():
    from octop.mcp_catalog import naming

    with pytest.raises(naming.NamingError, match="console service list failed"):
        naming.list_console_services(_ConsoleNamingStub(fail=True),
                                     namespace="l", group="g")


def test_source_services_endpoint_dispatch(monkeypatch):
    from fastapi import HTTPException

    from octop.mcp_catalog import router

    # 2026-10-15 起 naming 查询走 Console 聚合视图（nacos2 直查退役——
    # v3 admin API 与 MSE nacos2 不兼容）；stub 只喂 list_services
    stub = _ConsoleNamingStub(rows=_CONSOLE_ROWS)
    monkeypatch.setattr(router, "gateway_client_from_env", lambda: stub)
    monkeypatch.setattr(router, "resolve_registry",
                        lambda c, n: {"name": "nacos", "type": "nacos2",
                                      "nacos_namespace_id": "local",
                                      "nacos_group": "DEFAULT_GROUP"})
    out = router.list_source_services("nacos", None)
    assert out == {"services": ["summer-laac"], "registry_type": "nacos2"}

    inst = router.get_source_instance("nacos", "summer-laac", None)
    assert inst == {"instance": {"ip": "192.168.5.20", "port": 48084,
                                 "namespace": "local", "group": "DEFAULT_GROUP"}}

    monkeypatch.setattr(router, "resolve_registry",
                        lambda c, n: {"name": "static-x", "type": "static", "domain": "h:1"})
    with pytest.raises(HTTPException) as ei:
        router.list_source_services("static-x", None)
    assert ei.value.status_code == 400


# ------------------------------------------------- v2 P4: 导入与发布 diff

def _openapi_spec(paths, title="spec"):
    ops = []
    for p, method, name in paths:
        ops.append(
            f'"/v1{p}": {{"{method.lower()}": {{"operationId": "{name}", '
            f'"responses": {{"200": {{"description": "ok"}}}}}}}}'
        )
    return {
        "openapi": "3.0.0",
        "info": {"title": title, "version": "1.0"},
        "paths": json.loads("{" + ",".join(ops) + "}"),
    }


def test_import_openapi_body_channel_and_dry_run(repo: CatalogRepo):
    """P4.1 粘贴通道（无 URL）+ P4.2 dry-run：预览分类 imported/overwritten/
    deleted 且零落库；manual 工具永不被回收。"""
    import types as _types

    from octop.mcp_catalog import router

    repo.create_service(_svc_fields())
    # 既有 openapi 工具（将被覆盖）+ 既有 manual 工具（永不回收）
    repo.upsert_tool({**_tool_fields(), "source": "openapi"})
    repo.upsert_tool({**_tool_fields(tool_name="hand_made", http_path="/hand"),
                      "source": "manual"})
    server = _types.SimpleNamespace(services=_types.SimpleNamespace(db=repo._db))

    # dry-run：spec 含同名工具（改了 path）+ 一个新工具
    spec = _openapi_spec([("/banks", "get", "list_cmc_banks"), ("/new", "get", "brand_new")])
    out = router.import_openapi(
        "summer-cmc",
        router.ImportOpenApiBody(openapi_body=json.dumps(spec), dry_run=True),
        None,
        server,
    )
    assert out["dry_run"] is True
    assert out["imported"] == ["brand_new"]
    assert out["overwritten"] == ["list_cmc_banks"]
    assert out["deleted"] == []  # 两工具都在 spec 里，无回收
    # 零落库：同名工具 path 未变、新工具不存在
    assert repo.get_tool("summer-cmc", "list_cmc_banks").http_path == "/rpc-api/cmc/bank-info/banks"
    assert repo.get_tool("summer-cmc", "brand_new") is None

    # 正式导入：body 通道（无 URL）落库；spec 外的 openapi 工具被回收
    spec2 = _openapi_spec([("/banks-v2", "get", "list_cmc_banks")])
    out2 = router.import_openapi(
        "summer-cmc", router.ImportOpenApiBody(openapi_body=json.dumps(spec2)), None, server
    )
    assert out2["overwritten"] == ["list_cmc_banks"]
    assert repo.get_tool("summer-cmc", "list_cmc_banks").http_path == "/v1/banks-v2"
    assert repo.get_tool("summer-cmc", "brand_new") is None
    assert repo.get_tool("summer-cmc", "hand_made") is not None  # manual 幸存


def test_publish_diff_against_live_plugin_config(repo: CatalogRepo, monkeypatch):
    """P4.3：desired=DB(enabled 工具经 build_plugin_tool)，live=路由级
    mcp-server 实例的 configurations；canonical 比较 → added/updated/removed/
    unchanged 精确。"""
    import types as _types

    from octop.mcp_catalog import router
    from octop.mcp_catalog.publisher import build_plugin_tool

    repo.create_service(_svc_fields())
    repo.upsert_tool(_tool_fields())  # list_cmc_banks POST /rpc-api/...
    desired = build_plugin_tool(repo.list_tools("summer-cmc")[0])

    live_entry = json.loads(json.dumps(desired))  # 发布后原样落网关
    live_configurations = {
        "server": {"name": "summer-cmc", "type": "rest"},
        "tools": [live_entry, {"name": "stale_tool"}],
    }

    class _LiveGateway:
        def __init__(self, configurations):
            self._configurations = configurations

        def get_route_plugin(self, route, plugin):
            return self._configurations

    monkeypatch.setattr(
        router, "gateway_client_from_env", lambda: _LiveGateway(live_configurations)
    )
    server = _types.SimpleNamespace(services=_types.SimpleNamespace(db=repo._db))
    out = router.publish_diff("summer-cmc", None, server)
    assert out == {"published": True, "added": [], "updated": [],
                   "removed": ["stale_tool"], "unchanged": ["list_cmc_banks"]}

    # 改 path → updated 出现该工具
    repo.upsert_tool({**_tool_fields(), "http_path": "/rpc-api/cmc/v2"})
    out2 = router.publish_diff("summer-cmc", None, server)
    assert out2["updated"] == ["list_cmc_banks"]
    assert out2["unchanged"] == []

    # 网关无实例 → published=False，全部为 added
    monkeypatch.setattr(router, "gateway_client_from_env", lambda: _LiveGateway(None))
    out3 = router.publish_diff("summer-cmc", None, server)
    assert out3["published"] is False and out3["added"] == ["list_cmc_banks"]


# ------------------------------------------------- v2 追加: 一键接入连接器

def _connector_server(repo: CatalogRepo):
    import types as _types

    from octop.infra.db.repos.settings import SettingsRepo

    return _types.SimpleNamespace(
        services=_types.SimpleNamespace(db=repo._db, settings_repo=SettingsRepo(repo._db))
    )


class _FakeConnectorService:
    """Records put_custom_servers payloads without touching the connectors repo."""

    def __init__(self, existing=None):
        self.existing = existing or {}
        self.saved = None
        self.user_id = None

    def get_custom_servers(self, user_id):
        return dict(self.existing)

    def put_custom_servers(self, user_id, servers, org_id=None):
        from octop.infra.connectors.custom_mcp import validate_servers_map

        self.user_id = user_id
        self.saved = validate_servers_map(servers)
        self.existing = dict(self.saved)
        return self.saved


def test_connector_settings_roundtrip_and_validation(repo: CatalogRepo):
    from fastapi import HTTPException

    from octop.mcp_catalog import router

    server = _connector_server(repo)
    assert router.get_connector_settings(None, server)["higress_base"] == ""
    router.put_connector_settings(
        router.ConnectorSettingsBody(higress_base="http://172.17.0.1/"), None, server
    )
    # 尾部斜杠归一化
    assert router.get_connector_settings(None, server)["higress_base"] == "http://172.17.0.1"
    for bad in ("ftp://x", "not-a-url", "http://x/?a=1"):
        with pytest.raises(HTTPException) as ei:
            router.put_connector_settings(router.ConnectorSettingsBody(higress_base=bad), None, server)
        assert ei.value.status_code == 400


def test_connector_settings_hub_endpoint_roundtrip_and_validation(repo: CatalogRepo):
    """hub_endpoint KV 往返：合法值落库可读回、非法格式 400、空串清除。"""
    from fastapi import HTTPException

    from octop.mcp_catalog import router

    server = _connector_server(repo)
    assert router.get_connector_settings(None, server)["hub_endpoint"] == ""
    router.put_connector_settings(
        router.ConnectorSettingsBody(
            higress_base="http://172.17.0.1", hub_endpoint="192.168.133.204:8090"
        ),
        None,
        server,
    )
    assert router.get_connector_settings(None, server)["hub_endpoint"] == "192.168.133.204:8090"
    for bad in ("no-port", "host:", "host:0", "host:99999", "host:name"):
        with pytest.raises(HTTPException) as ei:
            router.put_connector_settings(
                router.ConnectorSettingsBody(higress_base="http://172.17.0.1", hub_endpoint=bad),
                None,
                server,
            )
        assert ei.value.status_code == 400
    # 非法值不落库；None = 不改动
    assert router.get_connector_settings(None, server)["hub_endpoint"] == "192.168.133.204:8090"
    router.put_connector_settings(
        router.ConnectorSettingsBody(higress_base="http://172.17.0.1"), None, server
    )
    assert router.get_connector_settings(None, server)["hub_endpoint"] == "192.168.133.204:8090"
    # "" = 重置回冻结默认
    router.put_connector_settings(
        router.ConnectorSettingsBody(higress_base="http://172.17.0.1", hub_endpoint=""),
        None,
        server,
    )
    assert router.get_connector_settings(None, server)["hub_endpoint"] == ""


def test_attach_connector_gates(repo: CatalogRepo, monkeypatch):
    """draft 拒绝 409；未配 Higress 地址拒绝 400（fail-closed）。"""
    from fastapi import HTTPException

    from octop.mcp_catalog import router

    server = _connector_server(repo)
    monkeypatch.setattr(router, "_connector_service", lambda s: _FakeConnectorService())
    repo.create_service(_svc_fields())
    with pytest.raises(HTTPException) as ei:
        router.attach_connector("summer-cmc", router.AttachConnectorBody(), None, server)
    assert ei.value.status_code == 409
    repo.update_service("summer-cmc", {"status": "published"})
    with pytest.raises(HTTPException) as ei:
        router.attach_connector("summer-cmc", router.AttachConnectorBody(), None, server)
    assert ei.value.status_code == 400
    assert "connector-settings" in ei.value.detail


def test_attach_connector_per_service_row_carries_attribution(
    repo: CatalogRepo, monkeypatch
):
    """2026-10-05 reshape：一键接入改走与租户一致的「每服务一行」附件行——
    连接器行带上服务的归属（org_id）与角色（allowed_roles，随服务编辑单向
    同步）；旧「custom-mcp 容器行内条目」形态（无归属无角色的根因）在重挂
    时被迁移清除；重复 attach 原地更新不产生第二行。"""
    import types as _types

    from octop.config import OctopConfig
    from octop.infra.connectors.service import ConnectorService
    from octop.infra.db.repos.connectors import ConnectorRepo
    from octop.infra.db.repos.secrets import SecretRepo
    from octop.infra.db.repos.settings import SettingsRepo
    from octop.mcp_catalog import router

    server = _connector_server(repo)
    conn_svc = ConnectorService(
        repo=ConnectorRepo(repo._db),
        secret_repo=SecretRepo(repo._db),
        settings_repo=SettingsRepo(repo._db),
        config=OctopConfig(),
    )
    monkeypatch.setattr(router, "_connector_service", lambda s: conn_svc)
    router.put_connector_settings(
        router.ConnectorSettingsBody(higress_base="http://172.17.0.1"), None, server
    )
    repo.create_service(
        _svc_fields(org_id=None, allowed_roles=["r1", "r2"])
    )
    repo.update_service("summer-cmc", {"status": "published"})
    # connectors.user_id 外键：落一行调用者
    with repo._db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users(id, username, password_hash, role, created_at) "
            "VALUES (7, 'admin7', 'x', 'admin', 1)"
        )
    # 旧形态遗留：容器行里躺着 reshape 前一键接入写入的条目（另有手工连接器）
    conn_svc.put_custom_servers(
        7,
        {
            "mcp-summer-cmc": {
                "transport": "streamable_http",
                "url": "http://172.17.0.1/api/internal/mcp/summer-cmc",
            },
            "manual-1": {
                "transport": "streamable_http",
                "url": "http://192.168.1.5/mcp",
            },
        },
    )
    user = _types.SimpleNamespace(id=7)

    out = router.attach_connector(
        "summer-cmc", router.AttachConnectorBody(), user, server
    )
    assert out["action"] == "created"
    assert out["server_name"] == "mcp-summer-cmc"
    assert out["url"] == "http://172.17.0.1/api/internal/mcp/summer-cmc"
    rows = conn_svc._repo.list_by_kind("custom-mcp:summer-cmc")
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == 7
    assert row.org_id is None  # 服务归属原样继承（平台私有服务 → NULL）
    assert row.allowed_roles == ["r1", "r2"]  # 服务角色同步到行
    assert row.shared  # harness 挂载/选择器链路按共享附件行读取
    # 旧容器条目被迁移清除，手工连接器保留
    assert conn_svc.get_custom_servers(7).get("mcp-summer-cmc") is None
    assert "manual-1" in conn_svc.get_custom_servers(7)

    out2 = router.attach_connector(
        "summer-cmc", router.AttachConnectorBody(), user, server
    )
    assert out2["action"] == "updated"
    assert len(conn_svc._repo.list_by_kind("custom-mcp:summer-cmc")) == 1


def test_attach_connector_shared_service_row_stays_private(
    repo: CatalogRepo, monkeypatch
):
    """'*'（平台共享）服务的一键接入行降级为 org_id=NULL：管理员自用的
    附件行不得变成平台共享——否则绕过了租户在自己目录页的显式 opt-in。"""
    import types as _types

    from octop.config import OctopConfig
    from octop.infra.connectors.service import ConnectorService
    from octop.infra.db.repos.connectors import ConnectorRepo
    from octop.infra.db.repos.secrets import SecretRepo
    from octop.infra.db.repos.settings import SettingsRepo
    from octop.mcp_catalog import router

    server = _connector_server(repo)
    conn_svc = ConnectorService(
        repo=ConnectorRepo(repo._db),
        secret_repo=SecretRepo(repo._db),
        settings_repo=SettingsRepo(repo._db),
        config=OctopConfig(),
    )
    monkeypatch.setattr(router, "_connector_service", lambda s: conn_svc)
    router.put_connector_settings(
        router.ConnectorSettingsBody(higress_base="http://172.17.0.1"), None, server
    )
    repo.create_service(_svc_fields(org_id="*", allowed_roles=[]))
    repo.update_service("summer-cmc", {"status": "published"})
    with repo._db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users(id, username, password_hash, role, created_at) "
            "VALUES (7, 'admin7', 'x', 'admin', 1)"
        )
    user = _types.SimpleNamespace(id=7)

    router.attach_connector("summer-cmc", router.AttachConnectorBody(), user, server)
    (row,) = conn_svc._repo.list_by_kind("custom-mcp:summer-cmc")
    assert row.org_id is None


def test_import_openapi_renames_cross_service_tool_name_collision(
    repo: CatalogRepo, monkeypatch
):
    """跨服务裸名冲突（review 2026-10-02 P1-3 的导入侧补丁）：hub ACL 以裸
    tool_name 跨服务投影，两个服务不得同名——但 springdoc 全量文档必然撞
    （每个 bank-info 控制器都有 list_banks）。导入侧对撞名的「新来方」
    确定性改名 _1/_2（springdoc 自身对文档内重复 operationId 的同款约定），
    而不是让整个一键导入失败；改名进报告、保持全局唯一不变量。"""
    import types as _types

    from octop.mcp_catalog import router

    repo.create_service(_svc_fields())
    repo.upsert_tool(
        _tool_fields(service_id="summer-cmc", tool_name="list_banks")
    )
    repo.create_service(
        _svc_fields(service_id="summer-laac", nacos_service_name="summer-laac")
    )

    spec = {
        "openapi": "3.0.1",
        "paths": {
            "/rpc-api/laac/bank-info/banks": {
                "post": {
                    "operationId": "list_banks",
                    "requestBody": {
                        "content": {"application/json": {"schema": {"type": "object"}}}
                    },
                }
            },
            "/rpc-api/laac/pre-approval/dealer/search": {
                "post": {
                    "operationId": "search_dealers",
                    "requestBody": {
                        "content": {"application/json": {"schema": {"type": "object"}}}
                    },
                }
            },
        },
    }

    class _FakeResp:
        def __init__(self, payload):
            self._p = json.dumps(payload).encode()

        def read(self):
            return self._p

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=15: _FakeResp(spec))
    server = _types.SimpleNamespace(services=_types.SimpleNamespace(db=repo._db))

    result = router.import_openapi(
        "summer-laac",
        router.ImportOpenApiBody(openapi_url="http://spec.local/v3/api-docs"),
        None,
        server,
    )
    # 撞名方被改名导入，干净方原名导入
    assert result["imported"] == ["list_banks_1", "search_dealers"]
    assert result["renamed"] == [
        {"from": "list_banks", "to": "list_banks_1", "owned_by": "summer-cmc"}
    ]
    # 新名归属 summer-laac，summer-cmc 原工具未被触碰
    assert repo.get_tool("summer-laac", "list_banks_1") is not None
    assert repo.get_tool("summer-laac", "list_banks") is None
    cmc_tool = repo.get_tool("summer-cmc", "list_banks")
    assert cmc_tool is not None and cmc_tool.http_path == "/rpc-api/cmc/bank-info/banks"
    # 全局裸名唯一（hub ACL 不变量）
    owners = {
        row.service_id
        for row in repo.list_tools("summer-cmc") + repo.list_tools("summer-laac")
        if row.tool_name in ("list_banks", "list_banks_1")
    }
    assert owners == {"summer-cmc", "summer-laac"}

    # 幂等重导入：再次撞名仍解析到同一新名，且保留管理员配置的角色/启用位
    repo.upsert_tool(
        {
            **_tool_fields(service_id="summer-laac", tool_name="list_banks_1"),
            "http_path": "/rpc-api/laac/bank-info/banks",
            "allowed_roles": ["t_cheryfs_retail_manager_level1"],
            "source": "openapi",
        }
    )
    result2 = router.import_openapi(
        "summer-laac",
        router.ImportOpenApiBody(openapi_url="http://spec.local/v3/api-docs"),
        None,
        server,
    )
    assert result2["imported"] == []
    assert result2["overwritten"] == ["list_banks_1", "search_dealers"]
    assert result2["renamed"] == [
        {"from": "list_banks", "to": "list_banks_1", "owned_by": "summer-cmc"}
    ]  # 改名确定性：重导入仍解析到同一新名
    kept = repo.get_tool("summer-laac", "list_banks_1")
    assert kept is not None and kept.allowed_roles == ["t_cheryfs_retail_manager_level1"]


def test_import_openapi_skips_collision_without_room_to_rename(
    repo: CatalogRepo, monkeypatch
):
    """改名后超 64 字符上限时不得写出非法名——该 op 落 skipped（不静默丢弃）。"""
    import types as _types

    from octop.mcp_catalog import router

    repo.create_service(_svc_fields())
    long_name = "a" * 63  # + "_1" → 65 > 64
    repo.upsert_tool(_tool_fields(service_id="summer-cmc", tool_name=long_name))
    repo.create_service(
        _svc_fields(service_id="summer-laac", nacos_service_name="summer-laac")
    )

    spec = {
        "openapi": "3.0.1",
        "paths": {
            "/rpc-api/laac/bank-info/banks": {
                "post": {
                    "operationId": long_name,
                    "requestBody": {
                        "content": {"application/json": {"schema": {"type": "object"}}}
                    },
                }
            },
        },
    }

    class _FakeResp:
        def __init__(self, payload):
            self._p = json.dumps(payload).encode()

        def read(self):
            return self._p

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=15: _FakeResp(spec))
    server = _types.SimpleNamespace(services=_types.SimpleNamespace(db=repo._db))
    result = router.import_openapi(
        "summer-laac",
        router.ImportOpenApiBody(openapi_url="http://spec.local/v3/api-docs"),
        None,
        server,
    )
    assert result["imported"] == []
    assert result["renamed"] == []
    assert len(result["skipped"]) == 1
    assert "cross-service tool name collision" in result["skipped"][0]["reason"]
    assert repo.get_tool("summer-laac", f"{long_name}_1") is None
