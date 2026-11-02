# MOSS Agent 基线评测报告

- 模型：`agnes-3.0-flash`（估算单价 输入 $0.15/M · 输出 $0.6/M）
- 时间：20261020_192030 · 用户：`test` · base：`http://127.0.0.1:8080`
- 任务数：8 · ✅ 通过 5 · ❌ 失败 1 · ⚠️ 错误 2

| 任务 | 能力档 | 成本档 | 结果 | 耗时(s) | 输入tok | 输出tok | 估算$ |
|---|---|---|---|---|---|---|---|
| c1_py_exec | C1 代码执行 | 中 | pass | 3.7 | 14794 | 5 | 0.002222 |
| c2_file_io | C2 文件读写 | 中 | pass | 136.33 | 43863 | 134 | 0.00666 |
| c3_analysis | C3 数据分析/计算 | 高 | pass | 36.07 | 43565 | 45 | 0.006562 |
| c4_retrieval | C4 信息检索 | 中 | pass | 51.21 | 28927 | 42 | 0.004364 |
| c5_network | C5 网络访问 | 高 | pass | 98.25 | 58151 | 102 | 0.008784 |
| c6_planning | C6 多步规划 | 高 | fail | 190.62 | 0 | 0 | 0.0 |
| c7_combo | C7 工具组合 | 高 | error | 240.03 | 29928 | 260 | 0.004645 |
| c8_precision | C8 输出精确性 | 低 | error | 90.08 | 29545 | 314 | 0.00462 |

## 汇总

- 总耗时：846.29s · 总 token：in 248773 / out 902 · 估算成本 $0.037857
- 通过率：5/8 = 62.5%
- 估算价 IN=$0.15/M OUT=$0.6/M（可覆盖）

## 明细

### c1_py_exec（C1 代码执行 · 结果 `pass` · 3.7s）
```
2550
```

### c2_file_io（C2 文件读写 · 结果 `pass` · 136.33s）
```
已创建文件 `bench_hello.txt`（工作区 `/root/agent_workspace/test`），并读取验证通过。

- 写入：`hello from moss bench`（22 字节）
- 第一行内容：**`hello from moss bench`**

验证结果：read_file 回读该文件第 1 行，输出与写入内容一致。
```

### c3_analysis（C3 数据分析/计算 · 结果 `pass` · 36.07s）
```
113.27
```

### c4_retrieval（C4 信息检索 · 结果 `pass` · 51.21s）
```
/opt/desktop-agent 最近一次提交时间（`git log -1 --format=%cs`）：

**2026-10-20**
```

### c5_network（C5 网络访问 · 结果 `pass` · 98.25s）
```
`status` 字段的值是：

**`"ok"`**
```

### c6_planning（C6 多步规划 · 结果 `fail` · 190.62s）
```
需要确认一下:第(2)(3)(4)步要写的文件位于工作区外(/tmp/moss_bench_planner),我这边没有授权无法直接写入。请在前端授权写入后回复"继续",我会重试。
```

### c7_combo（C7 工具组合 · 结果 `error` · 240.03s）
```
TimeoutError: timed out
```

### c8_precision（C8 输出精确性 · 结果 `error` · 90.08s）
```
TimeoutError: timed out
```
