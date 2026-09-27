# 离线 DNS 区域应答 API

基于 FastAPI 的离线 DNS 区域解析服务：在请求体中携带整个区域数据，对查询域名执行 **A 查询** 解析，或对 IPv4 地址执行 **PTR 反向查询**（`in-addr.arpa.`），支持 CNAME 链、通配符与 **NS 区域委派**。同时提供 **区域快照与条件更新** 模块：区域数据持久化在 SQLite 中，可按版本与前置条件进行原子更新，并对每个已提交版本留档，支持任意两个版本间的 **差异导出**；也可以把 **DNS 主文件（zone file）文本** 直接导入为版本 0 快照。

## 启动

单条命令（Docker Compose）：

```bash
docker compose up --build
```

服务在容器内监听 **8000** 端口，`docker-compose.yml` 将其映射到宿主机的 **8000** 端口（`http://localhost:8000`，交互式文档见 `http://localhost:8000/docs`）。如宿主机 8000 被占用，可把 compose 中的 `"8000:8000"` 改为 `"<其他端口>:8000"`。

Compose 同时把命名卷 `dns-data` 挂载到容器的 `/data`，并通过环境变量 `DNS_DB_PATH=/data/zones.db` 指定 SQLite 数据库位置；**表在服务启动时自动创建**，无需手工迁移，容器重启后区域快照仍然保留。

不使用 Docker 时：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

此时数据库默认写到当前目录的 `./zones.db`，可用 `DNS_DB_PATH` 环境变量改成其他路径。

## API

### `POST /resolve`

请求体：

```json
{
  "zone": "example.com",
  "records": [
    {"name": "www.example.com", "type": "A", "value": "192.0.2.1"},
    {"name": "www.example.com", "type": "A", "value": "192.0.2.2"},
    {"name": "alias.example.com", "type": "CNAME", "value": "www.example.com"},
    {"name": "*.wild.example.com", "type": "A", "value": "203.0.113.9"},
    {"name": "del.example.com", "type": "NS", "value": "ns1.del.example.com"},
    {"name": "del.example.com", "type": "NS", "value": "ns2.elsewhere.example.net"},
    {"name": "ns1.del.example.com", "type": "A", "value": "198.51.100.10"}
  ],
  "query": "alias.example.com",
  "qtype": "A"
}
```

| 字段 | 说明 |
| --- | --- |
| `zone` | 区域名（如 `example.com`） |
| `records` | 区域记录列表，支持 `A`、`CNAME`、`NS` 与 `PTR` |
| `query` | 查询域名 |
| `qtype` | 查询类型，可选，仅支持 `A`（默认值），其他类型返回 422 |

普通解析响应（`200 OK`）：

```json
{
  "zone": "example.com.",
  "query": "alias.example.com.",
  "status": "NOERROR",
  "chain": ["alias.example.com.", "www.example.com."],
  "addresses": ["192.0.2.1", "192.0.2.2"],
  "delegation": null,
  "nameservers": [],
  "glue": [],
  "error": null
}
```

委派转介响应（`status` 为 `REFERRAL`，`addresses` 恒为空）：

```json
{
  "zone": "example.com.",
  "query": "host.del.example.com.",
  "status": "REFERRAL",
  "chain": ["host.del.example.com."],
  "addresses": [],
  "delegation": "del.example.com.",
  "nameservers": ["ns1.del.example.com.", "ns2.elsewhere.example.net."],
  "glue": [
    {"name": "ns1.del.example.com.", "type": "A", "addresses": ["198.51.100.10"]}
  ],
  "error": null
}
```

- `chain`：沿 CNAME 依次访问的域名（首元素为查询名，末元素为最终落点）；转介时末元素为跨入委派子树的名称。
- `addresses`：最终名称上的 IPv4 地址列表；`REFERRAL` 时恒为 `[]`。
- `delegation`：仅 `REFERRAL` 有值，命中的委派点（绝对域名）。
- `nameservers`：仅 `REFERRAL` 有值，委派点上去重并按字典序排序的 NS 目标（绝对域名）。
- `glue`：仅 `REFERRAL` 有值，位于本委派子树内的 NS 目标所对应的同名 A 记录；按 `nameservers` 顺序排列，地址去重排序。
- `status` 取值：

| status | 含义 |
| --- | --- |
| `NOERROR` | 解析成功，`addresses` 为最终 IPv4 地址 |
| `NODATA` | 名称存在（含仅有后代的空节点），但没有 A 记录 |
| `NXDOMAIN` | 名称不存在，且未被通配符匹配 |
| `CNAME_LOOP` | CNAME 成环，`chain` 为闭合链（末元素与环起点重复），`error` 含说明 |
| `REFUSED` | 查询域名不在本区域内 |
| `REFERRAL` | 查询命中委派点或其子树，返回委派点、NS 目标与 glue，不做权威应答 |

区域数据非法（NS 与同名 A/CNAME/PTR 共存、顶点或通配符 NS、嵌套委派、CNAME 与其他记录同名、CNAME 目标在区域外、IPv4 格式错误、记录名在区域外、非 A 查询、PTR 所有者名非法或与 CNAME/NS 同名等）返回 `422` 及错误详情。

### 解析规则

- 域名忽略大小写，统一规范为末尾带点的小写形式。
- 同名允许多条 A 记录；CNAME 不能与任何其他记录（含另一条 CNAME）同名。
- CNAME 目标必须位于本区域内。
- 先查精确名称；不存在时，找最长的已存在祖先（最近封闭点），再查该祖先下的通配符 `*.<祖先>`。
- 有后代记录却没有自身记录的空节点也算存在：它会阻断上层通配符的匹配。
- 命中 CNAME 时继续解析其目标，返回完整别名链与最终 IPv4 地址。

### 区域委派（NS / REFERRAL）

- NS 记录可放在**非顶点、非通配符**的名称上，同一委派点允许放多条 NS。
- NS 目标统一规范化为绝对域名（小写、末尾带点），**可以位于本区域外**。
- 委派点上的 NS 不得与同名的 A/CNAME 共存；委派不得嵌套（一个委派点不能落在另一个委派点的子树内），否则返回 `422`。
- **先判委派边界**：查询名等于某委派点或位于其子树时直接返回 `REFERRAL`，该判断优先于精确 A 记录和上层通配符，二者都无法绕过转介。
- NS 目标若位于本委派子树内，可用同名 A 记录提供 **glue**；glue 只随转介返回。对 glue 名称（以及子树内任何名称）直接做 A 查询时仍返回 `REFERRAL`，不会把 glue 地址当作权威 A 应答。
- 子树内除 glue 外的其他记录（A/CNAME 等）不是本区域的权威数据，不参与应答。
- 区域外（或不在本子树内）的 NS 目标不带 glue。
- CNAME 链在跟随过程中跨入委派子树（包括正好落在委派点）时，保留已走过的链并返回 `REFERRAL`。

### `POST /reverse`

对 IPv4 地址做 **PTR 反向查询**。服务把 `address` 的四个八位组倒序拼成 `<d>.<c>.<b>.<a>.in-addr.arpa.` 查询名，在请求携带的反向区域内解析 PTR 记录。请求体：

```json
{
  "zone": "168.192.in-addr.arpa",
  "records": [
    {"name": "10.1.168.192.in-addr.arpa", "type": "PTR", "value": "host.example.com."},
    {"name": "10.1.168.192.in-addr.arpa", "type": "PTR", "value": "alias.example.com."}
  ],
  "address": "192.168.1.10"
}
```

| 字段 | 说明 |
| --- | --- |
| `zone` | 反向区域，限 `in-addr.arpa.` 本身或其**至多四段**合法 IPv4 数字标签子域（每段 `0`～`255`、无前导零），如 `192.in-addr.arpa`、`168.192.in-addr.arpa` |
| `records` | 区域记录列表，规则同 `POST /resolve`，可含 `PTR` |
| `address` | 待反查的 IPv4 地址（点分四段） |

正常响应（`200 OK`）：

```json
{
  "zone": "168.192.in-addr.arpa.",
  "address": "192.168.1.10",
  "query": "10.1.168.192.in-addr.arpa.",
  "status": "NOERROR",
  "chain": ["10.1.168.192.in-addr.arpa."],
  "hostnames": ["alias.example.com.", "host.example.com."],
  "delegation": null,
  "nameservers": [],
  "glue": [],
  "error": null
}
```

- `query`：由 `address` 生成的反查域名（八位组倒序、小写、末尾带点）。
- `hostnames`：最终名称上**去重并按字典序排序**的 PTR 目标主机名；`REFERRAL` 时恒为 `[]`。
- `chain`、`delegation`、`nameservers`、`glue`、`error` 语义与 `POST /resolve` 相同。
- `status` 取值与 `POST /resolve` 相同：`NOERROR`（命中 PTR）、`NODATA`（名称存在但没有 PTR 记录）、`NXDOMAIN`（名称不存在）、`CNAME_LOOP`、`REFUSED`（反查名在区域外）、`REFERRAL`（命中委派，不返回主机名）。

PTR 记录规则：

- **所有者名**必须是区域内**完整的四段反序 IPv4 名**（`<d>.<c>.<b>.<a>.in-addr.arpa.`，每段 `0`～`255`、无前导零），**不允许通配符**。
- 同名允许**多条 PTR**；目标规范化为绝对域名（小写、末尾带点），**可位于区域外**。
- PTR 不得与 CNAME/NS 同名（与 A 同名允许）。
- 通配符匹配、空节点、CNAME 链与委派（REFERRAL/glue）规则与正向解析完全一致；委派子树内的 PTR 记录不是权威数据。
- 非法地址、非法反向区域或非法记录返回 `422`。

PTR 记录同样可经 `POST /zones`、`POST /zones/{id}/updates` 与 `POST /zones/import` 进入快照并随版本留档（导入时无尾点的 PTR 目标相对当前 origin 展开），可参与 `GET /zones/{id}/diff` 差异导出。

### `POST /zones`

用 `zone` 与 `records` 建立 SQLite 区域快照。请求体与 `POST /resolve` 的前两个字段相同：

```json
{
  "zone": "example.com",
  "records": [
    {"name": "www.example.com", "type": "A", "value": "192.0.2.1"}
  ]
}
```

- 记录先做规范化（域名小写带尾点、类型大写、IPv4 规范化、NS/CNAME/PTR 目标转绝对域名）并去重，再按与 `/resolve` 相同的规则校验整个区域；非法输入返回 `422`。
- **委派子树内的输入记录原样保留**（glue 及其他暂非权威的记录都存入快照），不经过解析时的裁剪。
- 响应 `200`：`{"id": "...", "version": 0, "zone": "example.com.", "records": [...]}`，其中 `records` 为规范化、去重后按 `(name, type, value)` 排序的完整快照。

### `POST /zones/import`

把 **DNS 主文件（zone file）文本** 解析并导入为版本 `0` 的快照。请求体为 `zone` 与 `text` 两个字段：

```json
{
  "zone": "example.com",
  "text": "$TTL 3600\n$ORIGIN example.com.\n@    A    192.0.2.1\nwww  A    192.0.2.2\n     A    192.0.2.3\nalias CNAME www\ndel  NS    ns1.del\nns1.del A 198.51.100.10\n"
}
```

支持的语法（RFC 1035 主文件的严格子集）：

- 仅支持 `A`、`CNAME`、`NS`、`PTR` 四种记录类型（大小写不限），其他类型一律拒绝。
- `;` 起到行尾为注释；空行被忽略。
- 括号 `(` `)` 允许一条逻辑记录跨越多行；括号不可嵌套，未闭合或不配对均报错。
- 初始 origin 为 `zone`；`$ORIGIN <名>` 切换当前 origin。
- `@` 表示当前 origin；无尾点的**记录名、CNAME/NS/PTR 目标与 `$ORIGIN` 参数**均相对当前 origin 展开，带尾点的名为绝对名。
- 逻辑记录首字符为空白（空格或制表符）时省略记录名，沿用上一条记录**已展开**的名称；切换 origin 不改变该名称；首条记录省名报错。
- 类型之前可省略或填写 `IN` 与非负十进制 TTL，二者顺序不限；`$TTL` 指令同理。**TTL 只校验不存储**。
- 引号、转义（`\`）以及其他 `$` 指令（如 `$INCLUDE`、`$GENERATE`）均拒绝。

行为：

- **语法错误**返回 `422`，`detail` 为 `{"line": <逻辑记录起始行号>, "message": "<原因>"}`；跨行记录的行号取其起始物理行。
- 解析出的记录复用与 `POST /zones` 完全相同的规范化、去重、排序与整体区域校验；区域非法返回 `422`（`detail` 为字符串）。**任何失败都不建快照**。
- 成功时响应与 `POST /zones` 相同：`{"id", "version": 0, "zone", "records"}`，版本 `0` 同时留档，可直接用于后续 `GET /zones/{id}`、`updates` 与 `diff`。

### `GET /zones/{id}`

读取快照，返回 `{"id", "version", "zone", "records"}`；`records` 可直接作为 `POST /resolve` 的 `records` 字段使用。未知 id 返回 `404`。

### `POST /zones/{id}/updates`

对区域做**条件更新**。请求体：

```json
{
  "expected_version": 0,
  "prerequisites": [
    {"name": "www.example.com", "type": "A", "values": ["192.0.2.1"]},
    {"name": "old.example.com", "type": "CNAME", "values": []}
  ],
  "operations": [
    {"op": "add", "name": "www.example.com", "type": "A", "value": "192.0.2.2"},
    {"op": "delete", "name": "www.example.com", "type": "A", "value": "192.0.2.1"},
    {"op": "delete_set", "name": "old.example.com", "type": "CNAME"}
  ]
}
```

- `expected_version`：期望的当前版本号；与存储版本不符返回 `409`。
- `prerequisites`：每个条件按 `name` + `type` 指定该记录集**完整的 `values` 集合**；空集合表示该记录集必须不存在。域名沿用现有规范化，`values` 去重后按集合比较；所有条件都针对**更新前**的快照检查，任一不满足返回 `409`。
- `operations`：按顺序执行。`add` 添加单条记录、`delete` 删除单条记录、`delete_set` 删除 `name` + `type` 指定的整个记录集；重复添加与删除缺项均无效果。`add`/`delete` 必须带 `value`。
- **允许中间状态暂时冲突**（如某名上同时存在 A 与 CNAME），只按现有区域规则校验**最终**区域；最终区域非法返回 `422`。
- 非法输入（域名/类型/IPv4 非法、未知操作、缺少 `value`、`expected_version` 为负等）返回 `422`。
- 失败（`409`/`422`）不修改任何数据与版本；成功则**原子提交**、版本号 +1 并返回新快照（格式同 `GET /zones/{id}`）。同一版本的并发更新**最多只有一个成功**，其余得到 `409`。
- 未知 id 返回 `404`。

### `GET /zones/{id}/diff`

导出两个版本间的**记录集差异**。查询参数：

| 参数 | 说明 |
| --- | --- |
| `from_version` | 起始版本号，非负整数，且不大于 `to_version` |
| `to_version` | 结束版本号，非负整数 |

```bash
curl -s "http://localhost:8000/zones/$ZID/diff?from_version=0&to_version=1"
```

响应 `200`：

```json
{
  "zone": "example.com.",
  "from_version": 0,
  "to_version": 1,
  "removed": [
    {"name": "www.example.com.", "type": "A", "value": "192.0.2.1"}
  ],
  "added": [
    {"name": "www.example.com.", "type": "A", "value": "192.0.2.2"}
  ]
}
```

- **版本留档**：`POST /zones` 创建时保存版本 `0` 的完整规范化记录；每次 `POST /zones/{id}/updates` 成功时，在**同一 SQLite 写事务**中保存新版本记录——更新失败（`409`/`422`）回滚，不会留下历史版本。留档内容含**委派子树记录**（glue 及其他暂非权威的记录），与快照一致。
- 差异按两端**完整记录集合**求差：`removed` 为起始版本有而结束版本没有的记录，`added` 反之；两者均按 `(name, type, value)` 排序。**同一版本对比时两者均为空**。
- 升级前已存在的区域以其**当前版本**建立首个留档基线，不补造更早的版本；查询未留档的版本返回 `404`。
- 历史随 SQLite 持久化，服务重启后仍可查询。
- 未知区域 id 或任一版本未留档返回 `404`；参数非法（负数、非整数、缺参、`from_version > to_version`）返回 `422`。

## 示例

普通 A / CNAME 查询：

```bash
curl -s http://localhost:8000/resolve \
  -H 'Content-Type: application/json' \
  -d '{
        "zone": "example.com",
        "records": [
          {"name": "www.example.com", "type": "A", "value": "192.0.2.1"},
          {"name": "alias.example.com", "type": "CNAME", "value": "WWW.Example.com"}
        ],
        "query": "ALIAS.example.COM"
      }'
```

查询命中委派子树，返回转介与 glue：

```bash
curl -s http://localhost:8000/resolve \
  -H 'Content-Type: application/json' \
  -d '{
        "zone": "example.com",
        "records": [
          {"name": "del.example.com", "type": "NS", "value": "ns1.del.example.com"},
          {"name": "del.example.com", "type": "NS", "value": "ns2.elsewhere.example.net"},
          {"name": "ns1.del.example.com", "type": "A", "value": "198.51.100.10"}
        ],
        "query": "www.del.example.com"
      }'
```

IPv4 地址反查（PTR，服务端口同为 **8000**）：

```bash
curl -s http://localhost:8000/reverse \
  -H 'Content-Type: application/json' \
  -d '{
        "zone": "168.192.in-addr.arpa",
        "records": [
          {"name": "10.1.168.192.in-addr.arpa", "type": "PTR", "value": "host.example.com."},
          {"name": "10.1.168.192.in-addr.arpa", "type": "PTR", "value": "alias.example.com."}
        ],
        "address": "192.168.1.10"
      }'
# => {"zone":"168.192.in-addr.arpa.","address":"192.168.1.10",
#     "query":"10.1.168.192.in-addr.arpa.","status":"NOERROR",
#     "chain":["10.1.168.192.in-addr.arpa."],
#     "hostnames":["alias.example.com.","host.example.com."],
#     "delegation":null,"nameservers":[],"glue":[],"error":null}
```

建立快照并做条件更新：

```bash
# 建立快照，记下返回的 id（假设为 $ZID），version 从 0 开始
curl -s http://localhost:8000/zones \
  -H 'Content-Type: application/json' \
  -d '{"zone": "example.com",
       "records": [{"name": "www.example.com", "type": "A", "value": "192.0.2.1"}]}'
# 读取快照
curl -s http://localhost:8000/zones/$ZID

# 条件更新：确认 www 的 A 集合仍是 {192.0.2.1}，然后替换为 192.0.2.2
curl -s http://localhost:8000/zones/$ZID/updates \
  -H 'Content-Type: application/json' \
  -d '{
        "expected_version": 0,
        "prerequisites": [
          {"name": "www.example.com", "type": "A", "values": ["192.0.2.1"]}
        ],
        "operations": [
          {"op": "delete", "name": "www.example.com", "type": "A", "value": "192.0.2.1"},
          {"op": "add", "name": "www.example.com", "type": "A", "value": "192.0.2.2"}
        ]
      }'

# 差异查询：比较版本 0 与版本 1 的完整记录集
curl -s "http://localhost:8000/zones/$ZID/diff?from_version=0&to_version=1"
# => {"zone":"example.com.","from_version":0,"to_version":1,
#     "removed":[{"name":"www.example.com.","type":"A","value":"192.0.2.1"}],
#     "added":[{"name":"www.example.com.","type":"A","value":"192.0.2.2"}]}
```

从主文件文本导入区域（同样得到版本 0 快照，记下返回的 id 即可接上面的读取/更新/差异命令）：

```bash
curl -s http://localhost:8000/zones/import \
  -H 'Content-Type: application/json' \
  -d '{
        "zone": "example.com",
        "text": "$TTL 3600\n$ORIGIN example.com.\n@    A     192.0.2.1\nwww  A     192.0.2.2\n     A     192.0.2.3\nalias CNAME www\ndel  NS    ns1.del\nns1.del A (\n  198.51.100.10 )\n"
      }'
# => {"id":"...","version":0,"zone":"example.com.","records":[
#      {"name":"alias.example.com.","type":"CNAME","value":"www.example.com."},
#      {"name":"del.example.com.","type":"NS","value":"ns1.del.example.com."},
#      {"name":"example.com.","type":"A","value":"192.0.2.1"},
#      {"name":"ns1.del.example.com.","type":"A","value":"198.51.100.10"},
#      {"name":"www.example.com.","type":"A","value":"192.0.2.2"},
#      {"name":"www.example.com.","type":"A","value":"192.0.2.3"}]}
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```
