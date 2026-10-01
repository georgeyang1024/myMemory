# ADR-0030 · `multi_user` 配置子项与管理员全域权限

**状态**：提案 · 2026-10-01（同一轮 grilling 的追加决策；开发细节见 [SPEC-MULTI-USER](../SPEC-MULTI-USER.md)）

**修订**：2026-10-01（实施前审查）——`multi_user` 增第三键
`guest_writable`（bool，默认 false）：访客会话对公共 source 一律只读，
即使公共 source 配了 `writable: true`。理由：多人形态让"匿名连接"从
"本机主人"变成"内网任何人"，匿名写团队公共记忆是新增风险面，默认关；
确有匿名写入需求再显式打开。实现落在 scoped_config 访客分支（各公共
Source 的 writable 置 false），写拒绝沿现有只读路径，零新代码。

**修订二**：2026-10-01——`multi_user` 增第四键 `enabled`（bool，**默认
false**）：multi_user 块可以预先配好而不启用，只有显式 `enabled: true`
才进入多人共用形态（开关关闭 = 单机形态，块内其余配置原样保留）。
同时 `admins` 的缺省值从 `[]` 改为 `["admin"]`（未显式给出名单时默认
管理员是 admin；显式给出——含空数组——则完全按给定值）。默认访客只读
（`guest_writable: false`）不变。CLI 对应：`multi-user set` 直接写
`enabled: true`；`multi-user enable` / `disable` 翻开关（其余配置保留）。

## 决策

1. **配置重组**：[ADR-0028](0028-multi-user-shared-deployment.md) 里的扁平字段
   `store_dir` 收进一个大配置子项 **`multi_user`**：

   ```json
   "multi_user": {
     "store_dir": "E:\\memories\\users",
     "admins": ["李四"]
   }
   ```

   `multi_user` 出现且非空 = 启用多人共用形态；内部 `store_dir` 必填、
   `admins` 可选（默认 `[]`）。个人根目录与管理员同属"多人共用"一个开关，
   不再存在"admins 配了但 store_dir 没配"的悬空状态。

2. **管理员 = 配置白名单**（`multi_user.admins`），**纯授权身份**：
   - 名字**不做** source 命名白名单校验（任意字符串，逐字匹配 `?user=` 值）；
     硬约束三条，全是防头注入的**传输层**底线（管理员名要经 `?user=` 逐字匹配后
     注入请求头），不是命名白名单：**不得含控制字符**（CR/LF/NUL 等）、
     **长度 ≤64**（与 user 参数上限一致）、**不得为编辑者保留字**
     `agent` / `scan` / `guest`（[ADR-0031](0031-editor-attribution.md)）。
   - **不要求**在个人根目录下有同名个人目录（撤回了"须有个人目录"的初答）——
     管理员可以是纯运维角色；有同名目录时自己的个人 source 自然包含在全域里。
   - 开通 = 改配置 + **重启生效**，比建目录郑重：folder-as-authorization
     证明不了"可信"，配置白名单可以。

3. **管理员会话 = 全域单会话**：`?user=<管理员名>` 的会话范围 =
   公共 source + **全部**个人 source；list-sources 全量、search/recent
   默认跨全部（管理员身份的自然含义：全域检索即需全域可见）。

4. **边界不变**：
   - 管理员对公共 source 照旧尊重 `writable`（不突破 ADR-0022 语义；
     需要 AI 维护的公共 source 显式配 `writable: true`）；
   - delete 照旧受 `allow_mcp_delete` 全局断路器约束（不豁免；merge 工具已
     移除，断路器只管 delete，见 [ADR-0032](0032-remove-merge-tool.md)）；
   - `/health` 的 admins 名单与用户清单仅管理员视角（`?user=<管理员名>`）可见；
     默认视角只给 `store_dir` 与 `guest_writable`，不对局域网暴露名单与开通情况。

## 理由

- 为什么白名单而非魔法 user 名 / 建目录：全域权 = 可读写所有人的记忆，
  授权动作必须重于建目录；魔法名可被内网任何人使用，等于人人可管。
- 为什么 admins 收进 `multi_user`：两者同属多人形态，配置结构即表达
  "没有多人形态就没有管理员"；单机形态天然无管理员概念。

## 后果

- 路由校验的合法名字集合 = **admins ∪ 个人目录名**；管理员豁免
  "目录存在"要求，未知用户 400 文案不变。
- 安全模型补充（在 ADR-0028 已知风险之上）：管理员可读写所有人的个人
  source；冒充风险同样适用于管理员名单——内网任何人改 URL 即可"成为
  管理员"。收紧路线同 ADR-0028（per-user token 时 admins 通道一并收紧）。
- 实现上管理员判定落在工具层（读注入头后比对 `config.multi_user.admins`），
  中间件只负责"名字是否合法"（admins ∪ 个人目录）与头注入，职责单一。
