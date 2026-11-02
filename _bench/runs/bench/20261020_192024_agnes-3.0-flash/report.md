# MOSS Agent 基线评测报告

- 模型：`agnes-3.0-flash`（估算单价 输入 $0.15/M · 输出 $0.6/M）
- 时间：20261020_192024 · 用户：`test` · base：`http://127.0.0.1:8080`
- 任务数：8 · ✅ 通过 5 · ❌ 失败 2 · ⚠️ 错误 1

| 任务 | 能力档 | 成本档 | 结果 | 耗时(s) | 输入tok | 输出tok | 估算$ |
|---|---|---|---|---|---|---|---|
| c1_py_exec | C1 代码执行 | 中 | pass | 5.61 | 14650 | 5 | 0.0022 |
| c2_file_io | C2 文件读写 | 中 | pass | 139.99 | 44004 | 60 | 0.006637 |
| c3_analysis | C3 数据分析/计算 | 高 | pass | 35.5 | 29016 | 86 | 0.004404 |
| c4_retrieval | C4 信息检索 | 中 | pass | 52.9 | 87195 | 131 | 0.013158 |
| c5_network | C5 网络访问 | 高 | pass | 58.0 | 14625 | 35 | 0.002215 |
| c6_planning | C6 多步规划 | 高 | fail | 230.15 | 44388 | 276 | 0.006824 |
| c7_combo | C7 工具组合 | 高 | fail | 237.2 | 0 | 0 | 0.0 |
| c8_precision | C8 输出精确性 | 低 | error | 90.09 | 29545 | 314 | 0.00462 |

## 汇总

- 总耗时：849.44s · 总 token：in 263423 / out 907 · 估算成本 $0.040058
- 通过率：5/8 = 62.5%
- 估算价 IN=$0.15/M OUT=$0.6/M（可覆盖）

## 明细

### c1_py_exec（C1 代码执行 · 结果 `pass` · 5.61s）
```
2550
```

### c2_file_io（C2 文件读写 · 结果 `pass` · 139.99s）
```
已完成：

1. **创建文件**：在当前工作目录创建了 `bench_hello.txt`，内容为 `hello from moss bench`。
2. **读取第一行**：该文件第一行内容为：

> hello from moss bench

---

可下载文件：
- bench_hello.txt: [下载](/artifacts/download?path=bench_hello.txt) (`bench_hello.txt`)
```

### c3_analysis（C3 数据分析/计算 · 结果 `pass` · 35.5s）
```
113.27
```

### c4_retrieval（C4 信息检索 · 结果 `pass` · 52.9s）
```
最近一次 git 提交时间为：

**2026-10-20**
```

### c5_network（C5 网络访问 · 结果 `pass` · 58.0s）
```
`http://127.0.0.1:8080/health` 的 status 字段值为：

- **status: ok**
```

### c6_planning（C6 多步规划 · 结果 `fail` · 230.15s）
```
该目录在工作区外，文件写入需要你在界面点击「授权写入」。

**需要你操作：** 请在界面点击「授权写入」按钮，然后输入「继续」。授权后我会用完全相同的写文件调用重试，完成 a.txt/b.txt 创建、Python 求和写入 sum.txt，并返回 sum.txt 内容。

（说明：`mkdir` 用 run_shell 能成功，但 write_file 写 /tmp 下的文件必须走授权流程，我不能用 shell 等方式绕过授权写文件。）
```

### c7_combo（C7 工具组合 · 结果 `fail` · 237.2s）
```
**平均值：38.5**

- 1..10 的平方列表：`[1, 4, 9, 16, 25, 36, 49, 64, 81, 100]`
- 已写入 `/tmp/moss_bench_squares.json`
- 读回后计算：`385 / 10 = 38.5`
```

### c8_precision（C8 输出精确性 · 结果 `error` · 90.09s）
```
TimeoutError: timed out
```
