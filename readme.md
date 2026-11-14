# reach · NeurIPS 2026 参会人触达系统

链路：arXiv 真实数据 → 姓名归一去重 → 个性化消息渲染 → dry-run 投递 → 唯一链接点击追踪 → 漏斗看板。

## 两个入口

| 文件 | 用途 | 打开方式 |
|---|---|---|
| **`reach.html`** | **单文件版（对外分发用这个）** | 直接发给任何人，双击打开即可，无需 Python、无需联网、零依赖 |
| `reach.py` | 服务端版（真·实时抓取 / 真·服务端追踪） | `python reach.py`，本地 8777 端口 |

## 单文件版说明（reach.html）

- 内嵌 **2026-11-14 从 arXiv Atom API 抓取的真实论文快照**（62 篇 / 276 位去重作者），双击打开即完整可用。
- 点击追踪落在本机浏览器（localStorage）：名片链接 `#/c/<hash>` 每次被打开都会记入漏斗；**跨人的真实追踪仍需 `reach.py` 服务端版**，页面底部有此说明。
- 「我的名片 / 模板」抽屉里可编辑自我介绍与邮件模板（占位符 `{name} {paper} {paper60} {paper80} {link} {profile}`），保存后所有草稿自动重算，配置同样存本机。
- 支持按数据源/数量载入人群、关键词筛选、CSV 导出、一键重置。

## 目录结构

```
reach.html   单文件分发版（唯一交付物，数据与逻辑都在里面）
reach.py     服务端开发版（真·实时 arXiv + 真·SQLite 点击追踪）
readme.md    本文档
```

## 怎么改

- **改文案 / 名片 / 邮件模板**：不用改文件，打开 `reach.html` 点右上角「我的名片 / 模板」，改完保存即生效（存本机）。
- **改界面样式或逻辑**：`reach.html` 是自包含的，直接用编辑器改里面的 `<style>` 和 `<script>`；arXiv 数据是文件里 `const SNAPSHOT = [...]` 这一行 JSON 数组，要换数据就替换这段（每项字段：`id / title / authors / published / cat / query`）。
- **需要重新抓一批 arXiv 数据**：跟我要一次即可，我会重新生成快照并替换进 `reach.html`（之前用的抓取脚本已清理，可随时重建）。

## 已知边界（TODO）

- 真实发信未接通：`reach.py` 的 `/api/send_all` 仍是 dry-run，需接 SMTP（如 smtp.qq.com:465 + 授权码）。
- 姓名归一目前只做大小写/空白；下一步做（姓, 名, 机构）三元组模糊归一。
- OpenReview / Semantic Scholar 多源合并未做（OpenReview 有反爬，需官方账号）。
