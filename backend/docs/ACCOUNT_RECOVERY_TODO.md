# 账号恢复与有效 RT 重新导入

## 已验证结论

- **有效 Refresh Token 可以纯 HTTP 完成续期。** 服务端向 `https://auth.x.ai/oauth2/token` 提交 OAuth `refresh_token` grant，不需要浏览器 TLS 或页面登录。
- **重新导入有效 RT 也可以纯 HTTP 完成。** 使用管理接口 `POST /api/admin/v1/accounts/import-json`，`provider` 传 `grok_build`；同一身份会更新原账号凭据并把认证状态恢复为 `active`，随后进入模型/额度同步流水线。
- **新 RT / SSO 可定向覆盖已有账号。** 使用 `POST /api/admin/v1/accounts/{id}/reauth`，系统会保留账号 ID 和路由配置、清空旧失败状态，并立即执行首次同步。
- **`invalid_grant`、`Refresh token has been revoked` 无法靠重试恢复。** 这表示上游已经撤销或不再接受原 RT，必须重新取得新 RT。
- 取得新 RT 的可行入口：Device OAuth 完成用户授权，或由仍有效的 Grok Web SSO 转换为 Build OAuth。授权页面本身不能被“纯 HTTP 后台请求”替代。

## 待办

1. 增加批量定向恢复接口：按账号 ID 批量提交新凭据，并按 `有效 / 撤销 / 暂时失败` 分类返回。
2. 增加恢复任务审计：记录账号 ID、恢复来源、OAuth 错误码和最终状态，不记录 RT/AT/SSO 明文。
3. 为 `invalid_grant` 账号提供批量 Device OAuth 引导；授权完成后自动覆盖原账号而不是创建重复账号。
4. Web→Build 转换后增加 Build 权限探测。OAuth 成功但 Build `/responses` 返回 403 时标记为“无 Build entitlement”，不要把它误判为 RT 失效。
