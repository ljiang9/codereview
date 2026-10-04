# codereview

`git diff --staged` 进，结构化代码评审出。

一个小巧的 LLM 代码评审工具：读取你的暂存区 diff，调用 OpenAI 兼容接口，输出按严重程度分级的评审表（blocker / major / minor / nit），最后给出一行结论：**可合并** 还是 **需修改后合并**。

## 为什么不直接问 ChatGPT？

| 痛点 | codereview 的做法 |
|---|---|
| 每次都要手写"帮我 review"prompt | 内置评审 prompt，强制模型输出结构化 JSON |
| 输出是散文，不好扫 | 表格输出：严重程度 \| 文件:行 \| 问题 \| 建议 |
| 想在 CI 里卡 blocker | `--strict`：有 blocker 就 exit 1，直接当 CI 门禁 |
| 只想看安全问题 | `--focus security` 让模型聚焦某个维度 |

## 安装

零依赖，Python 3.10+，标准库即可：

```bash
git clone https://github.com/ljiang9/codereview.git
cd codereview
export OPENAI_API_KEY="sk-..."   # 必需；兼容任何 OpenAI 兼容接口
# 可选：export OPENAI_BASE_URL="https://你的网关/v1"
```

## 快速开始

```bash
cd 你的项目
git add -p            # 暂存要评审的改动
python -m codereview  # 评审暂存区
```

示例输出：

```
diff 统计：1 个文件，+9 -2

严重程度 | 文件:行      | 问题                               | 建议
---------+---------------+------------------------------------+------------------------------------------
blocker  | app.py:12     | SQL 字符串拼接，存在注入风险       | 改用参数化查询，如 cursor.execute(…)
major    | app.py:18     | 裸 except 会吞掉 KeyboardInterrupt | 改为 except Exception 并记录日志

⛔ 结论：需修改后合并
```

## 用法

```
codereview [--all | --range R] [--severity LEVEL] [--focus DIM]
           [--strict] [--dry-run] [--json] [--lang {zh,en}]
```

| 选项 | 说明 |
|---|---|
| （无） | 评审已暂存（staged）的 diff |
| `--all` | 评审未暂存（工作区）的改动 |
| `--range HEAD~2` | 评审指定区间的 diff |
| `--severity major` | 只显示 major 及以上（blocker/major） |
| `--focus security` | 聚焦维度：`security` / `performance` / `style` |
| `--strict` | 发现 blocker 时 exit 1，可做 CI 门禁 |
| `--dry-run` | 只打印 prompt + diff 统计，不联网（调试 prompt 用） |
| `--json` | 机器可读 JSON 输出，方便接 CI/机器人 |
| `--lang en` | 英文界面和 prompt |

CI 示例（GitHub Actions）：

```yaml
- run: python -m codereview --strict --severity major
  env:
    OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

## 注意事项

- 模型可能**误报或漏报**，评审结果仅供参考，合并前请人工复核。
- diff 超过 3 万字符会被截断（会明确提示）。
- API Key 只从环境变量 / `--api-key` 读取，**绝不会打印到输出或日志**。
- 默认模型 `gpt-4o-mini`，可用 `--model` 或 `OPENAI_MODEL` 覆盖。

## License

MIT — 详见 [LICENSE](LICENSE)。
