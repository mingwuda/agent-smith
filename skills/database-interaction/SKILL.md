# Database Interaction Skill

## Description
让 Agent 具备与数据库自然语言交互的能力。用户可以用中文描述数据需求，Agent 自动查看表结构、生成 SQL、执行查询并解读结果。

## Trigger
- 用户提到"查数据库""查询表""数据统计""SQL""数据库里有什么"等与数据库相关的请求
- 用户发出数据查询、统计、分析类指令
- 用户要求列出数据库连接或表信息

## Instructions

### 1. 理解用户需求
- 解析用户想要什么数据、涉及哪些表、需要什么计算（统计、分组、排序等）
- 如果不确定涉及哪些表，先调用 `db_schema` 列出所有可用表
- 如果不确定用户要查哪个数据库，先调用 `db_connections` 查看所有可用数据库连接

### 2. 了解表结构
- **始终先调用 `db_schema`** 了解相关表的结构，包括列名、类型、主键
- 如果有多个相关表，分别查看它们的结构
- 注意表之间的关系（通过列名推断外键关系）
- 工具 `db_schema` 支持 `connection` 参数，如需查询非默认数据库，传入连接名：
  ```
  db_schema(table_name="orders", connection="prod_pg")
  db_schema(connection="prod_pg")  # 列出该数据库下的所有表
  ```

### 3. 生成并执行 SQL
- 根据表结构编写正确的 SQL 查询
- 使用 `db_query` 执行（自动经过权限检查）
- 工具 `db_query` 支持 `connection` 参数，如需查询非默认数据库，传入连接名：
  ```
  db_query(sql="SELECT * FROM users", connection="prod_pg")
  ```
- SQL 必须使用正确的表名和列名（注意大小写）
- 优先使用参数化查询

### 4. 解读结果
- 将查询结果用自然语言解读给用户
- 如果结果为空，解释可能的原因
- 如果结果过多，建议用户缩小查询范围
- 提供数据洞察，不只是罗列原始数据

### 5. 安全原则
- 只使用 SELECT 查询，不尝试写操作
- 如果用户要求修改数据，告知需要通过管理员审批
- 注意敏感数据，不在对话中暴露可能敏感的信息

### 6. 多数据库处理
- 如果用户指定了数据库名，确认当前连接是否正确
- 使用 `db_connections` 查看可用数据库连接

## Notes
- 所有 SQL 执行前会经过 `dbcli/auth.py` 的权限检查
- 查询结果最多返回 500 行（由权限配置决定）
- 列级和行级权限由管理员在 `permissions.yaml` 中配置
- 此 Skill 依赖 `database_tool.py` 中的三个工具：`db_schema`、`db_query`、`db_connections`
