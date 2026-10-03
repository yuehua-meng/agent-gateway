# Token 统计与计费

为了看清用量组成 → 解析兼容接口的 usage 并按尝试落库 → 看板能按项目、实际模型和候选展示输入、输出、缓存及推理 Token，无需业务埋点。

## 字段映射

| 上游 Chat Completions 响应 | SQLite attempts 列 | 看板含义 |
|---|---|---|
| `usage.prompt_tokens` | `prompt_tokens` | 输入 Token |
| `usage.completion_tokens` | `completion_tokens` | 输出 Token |
| `usage.prompt_tokens_details.cached_tokens` | `cached_tokens` | 输入中命中缓存的部分 |
| `usage.completion_tokens_details.reasoning_tokens` | `reasoning_tokens` | 输出中推理的部分 |

目前只适配这些兼容字段，不自动转换 Responses、DashScope 原生或 Anthropic 用量结构。不采集推理正文。实现入口是 [Engine.safe_usage](../gateway/engine.py)，仅白名单保留合法整数及已支持的嵌套明细。

## 不重复计数

例如输入 1000，其中缓存 600；输出 200，其中推理 80：

```text
总 Token = 输入 1000 + 输出 200 = 1200
缓存 600 已在输入里，推理 80 已在输出里，不能再相加。
```

每次上游尝试独立记账；如果重试前那次也返回有效 usage，会计入其已知用量。幂等重放不新增尝试，因此不会重复统计。

## 未知不等于零

- 明确返回整数 0：是已知的零。
- 缺字段、null、非法类型、负数、超出存储整数范围：作为未知处理。
- 子项超过已知父项，例如输入 100、缓存 200：该缓存字段作为未知；父项缺失时可保留合法子项，但不能据此推算父项。
- 一部分调用返回细分字段：显示已知合计并标注“部分未返回”；全部未返回时显示未知。
- 已结束的文本尝试才进入 Token 完整性统计。图片、运行中尝试单独计数，不混入缓存/推理缺失次数。
- `known_token_calls` 表示输入和输出都已返回；`cached_known_calls`、`reasoning_known_calls` 独立计数。主用量完整不意味着细分字段也完整。

## 费用口径

文本当前公式为：

```text
估算费用 = (输入 Token × 输入每百万单价 + 输出 Token × 输出每百万单价) / 1,000,000
```

单价来自候选配置中的 `input_per_million`、`output_per_million`，币种来自 `currency`。图片使用配置的 `image_price`。结果在尝试结束时落库，修改单价不会重新计算历史记录。

尚未单独计算缓存折扣、缓存写入价或价格分档；推理 Token 已在输出中，不额外再加一次。缺少必要用量或价格时显示未计价；明确未受理的尝试单列，费用记 0。不同币种不合并。看板估算不是供应商账单，也不是按金额强制限额。

## 历史数据与代码

启动时 [Store](../gateway/storage.py) 给旧表增加可空 `cached_tokens`、`reasoning_tokens` 列；老记录保持 null，不用当前模型或价格猜测历史数据。`actual_model` 保存尝试发生时配置的模型名，早期未采集的记录显示历史模型未记录。

[observe.py](../gateway/observe.py) 负责统计，[app.js](../web/app.js) 负责用量页、分组表和尝试详情。用量与费用按筛选范围内创建的操作及其尝试统计。一般尝试保留约 30 天，未知费用按既有策略继续保留；看板时间窗口最多 30 天。

验证见 [test_token_details.py](../tests/test_token_details.py) 和 [test_observe.py](../tests/test_observe.py)。真实厂商是否返回对应字段仍以实际响应为准。
