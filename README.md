# 特征工坊 · 特征工程与标注数据管理平台

面向特征注册与检索、衍生特征工程、时间窗口计算、特征回溯填充、样本分层抽取、标注任务派发、标注质量审核、特征血缘追踪与在线/离线特征服务的数据特征工程与标注管理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/quality_gate.py  质量阻断事实台账与报告成稿编排
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 特征注册 | `feature_reg` | 特征定义 | 特征编号、特征名称、数据类型 |
| 衍生工程 | `derive_rule` | 衍生规则 | 规则编号、输入特征、衍生函数 |
| 时间窗口 | `time_window` | 窗口模板 | 模板编号、窗口类型、窗口宽度 |
| 特征回溯 | `backfill_job` | 回溯任务 | 任务编号、目标特征、起始日期 |
| 样本分层 | `sample_strata` | 分层策略 | 策略编号、特征范围、分层字段 |
| 标注派发 | `label_task` | 标注任务 | 任务编号、样本范围、标注类型 |
| 标注审核 | `label_review` | 审核批次 | 批次编号、标注任务、审核人 |
| 一致性计算 | `consent_calc` | 一致性报告 | 报告编号、标注任务、评估指标 |
| 特征存储 | `feature_store` | 存储配置 | 配置编号、特征列表、存储引擎 |
| 在线服务 | `online_serv` | 服务端点 | 端点编号、特征列表、QPS上限 |
| 离线导出 | `offline_exp` | 导出管道 | 管道编号、特征范围、导出格式 |
| 特征血缘 | `feature_line` | 血缘链路 | 链路编号、上游特征、下游特征 |
| 特征监控 | `feature_mon` | 监控规则 | 规则编号、目标特征、监控类型 |
| 版本管理 | `version_mgr` | 特征版本 | 版本编号、关联特征、版本标签 |
| 数据切分 | `data_split` | 切分配置 | 配置编号、数据集、切分方式 |
| 特征目录 | `feat_catalog` | 目录索引 | 索引编号、目录名称、父目录 |
| 质量规则 | `quality_rule` | 质量检查 | 规则编号、目标特征、检查类型 |
| 标注词典 | `label_dict` | 标注项 | 词典编号、标签名称、标签值 |
| 批量接入 | `batch_load` | 接入任务 | 任务编号、数据源、目标特征 |
| 特征报告 | `report_task` | 分析报表 | 报告编号、报告类型、特征范围 |
| 调度编排 | `schedule_job` | 调度计划 | 计划编号、任务类型、关联对象 |
| 治理审计 | `govern_audit` | 审计记录 | 记录编号、审计对象、审计类型 |

## 质量阻断与报告去重

质量检查「确认阻断」不只是把状态改成已阻断，还会在 `app/services/quality_gate.py`
中登记一条**检查事实**，由它串起阻断、报告成稿与分区恢复：

- 同一事件（规则 + 检查类型 + 数据批 + 检查轮次）只对应一份检查事实，重复或并发
  确认都复用同一份事实，只认领一张报告草稿，不会反复占用草稿。
- 报告写入中断后，从原成稿动作（`重试写入` / `完成成稿`）重试，仍使用原草稿和
  原来那份事实，不会重新认领。
- 分区证据决定「实际接入范围」：只有取得可验证检查原证并提交恢复申请的分区才会
  解除阻断，缺证分区始终保持阻断。恢复申请在报告未补齐时也可以提交，可证分区当班
  即可接入，缺证分区留待下班继续办理。
- 检查事实同时写入质量检查行和报告行，报告页与工作台都按事实编号读取同一份检查
  依据；已有报告引用的特征范围会保留并合并，不会被覆盖。
- 修复源数据后发起新一批检查，会生成独立的数据批/轮次、独立的检查事实与报告，
  与上一笔阻断互不影响。

相关接口：

- `GET /api/quality-gate/facts?rule_id=` / `?report_id=`：检查依据与成稿进度。
- `GET /api/quality-gate/report/{report_id}`：报告页读取报告及其关联事实。
- 动作接口额外接受 `params`，例如
  `{"action": "确认阻断", "params": {"数据批": "B1", "检查轮次": "1", "检查类型": "空值检查", "拦截分区": "P;Q", "分区证据": "P:原证编号"}}`。
  台账侧动作包括 `修复分区`、`补齐证据`、`申请恢复`、`重试写入`、`完成成稿`、
  `核对进度`、`发起下一批检查`。

后端回归测试：`cd backend && .venv/bin/python -m unittest discover -s tests -v`

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数仓与数据湖
