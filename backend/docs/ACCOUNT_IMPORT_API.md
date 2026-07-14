# 账号 JSON 导入 API

管理员可通过 `POST /api/admin/v1/accounts/import-json` 直接提交账号参数。接口与控制台文件导入共用账号去重、加密存储和首次同步流程；响应中的 `synced` / `syncFailed` 表示首次凭据、额度及模型能力同步结果。

## 鉴权

先调用 `POST /api/admin/v1/auth/login` 获取管理员 `accessToken`，随后发送：

```http
Authorization: Bearer <admin-access-token>
Content-Type: application/json
```

请求体最大 30 MiB，单次最多 10,000 个账号。

## Grok Build

```json
{
  "provider": "grok_build",
  "accounts": [
    {
      "name": "build-01",
      "client_id": "optional-client-id",
      "access_token": "optional-access-token",
      "refresh_token": "refresh-token",
      "id_token": "optional-id-token",
      "token_type": "Bearer",
      "scope": "optional-scope",
      "expires_at": "2026-07-13T16:00:00Z",
      "expires_in": 3600,
      "email": "optional@example.com",
      "user_id": "optional-user-id",
      "principal_id": "optional-principal-id",
      "team_id": "optional-team-id"
    }
  ]
}
```

`access_token` 与 `refresh_token` 至少提供一个。只有刷新令牌时，首次同步会尝试换取访问令牌；失效或已撤销的刷新令牌仍会被上游拒绝。

## Grok Web

```json
{
  "provider": "grok_web",
  "accounts": [
    {
      "name": "web-01",
      "sso_token": "sso-token",
      "tier": "auto"
    }
  ]
}
```

`sso_token` 也可使用字段名 `token`。`tier` 可为 `auto`、`basic`、`super` 或 `heavy`。

## Grok Console

```json
{
  "provider": "grok_console",
  "accounts": [
    {
      "name": "console-01",
      "sso_token": "sso-token"
    }
  ]
}
```

## 定向恢复已有账号

当 RT 或 SSO 已轮换时，应使用账号 ID 定向覆盖，而不是把新 Token 当成新账号导入：

```http
POST /api/admin/v1/accounts/{id}/reauth
```

Build 请求体：

```json
{
  "refreshToken": "fresh-refresh-token",
  "accessToken": "optional-access-token",
  "clientId": "optional-client-id",
  "expiresAt": "2026-07-14T16:00:00Z"
}
```

Web / Console 请求体：

```json
{
  "ssoToken": "fresh-sso-token",
  "webTier": "super"
}
```

该接口保留原账号 ID、身份键、优先级和并发配置，清除旧认证失败、刷新失败和冷却状态，然后立即进入额度与模型同步。响应中的 `syncFailed` 大于零时，表示新凭据已加密保存，但上游验证仍未通过。

## 号池趋势

```http
GET /api/admin/v1/accounts/analytics?period=24h
```

`period` 支持 `24h`、`7d`、`30d`。服务每 15 分钟保存一次聚合快照，仅记录各 Provider 的状态、类型、等级和额度汇总，不记录账号凭据或单账号明细。

## 成功响应

```json
{
  "data": {
    "provider": "grok_build",
    "created": 1,
    "updated": 0,
    "synced": 1,
    "syncFailed": 0
  }
}
```

该接口属于管理员接口，不应暴露管理员令牌，也不要在日志、代码仓库或第三方系统中记录账号凭据。
