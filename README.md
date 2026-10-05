# ETF 数据拉取

`tushare_etf_api.py` 是 ETF 基础信息接口文件，完整覆盖 Tushare `etf_basic` 页面显示的筛选参数和 14 个返回字段，并自动翻页。`etf_ingest.py` 在此基础上关联基金投资类型，下载沪深 ETF 上市以来可获取的全部日线行情。首次运行从上市日期开始，按时间段请求并在返回达到单次 5000 行上限时继续拆分；再次运行只补新交易日。已退市 ETF 也可拉取。

## 安装与运行

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export TUSHARE_TOKEN='你的 Tushare Pro Token'
.venv/bin/python tushare_etf_api.py
.venv/bin/python etf_ingest.py --codes 510300.SH 518880.SH
```

基础信息默认分别查询上市、退市、待上市三个状态，拉取全部可得 ETF，写入 `data/etf_basic.csv`。例如只查已上市沪市 ETF：

```bash
.venv/bin/python tushare_etf_api.py --list-status L --exchange SH
```

可在 Python 中直接调用接口：

```python
from tushare_etf_api import fetch_etf_basic

df = fetch_etf_basic(list_status="L", exchange="SH")
```

`limit` 是每次请求的行数，最大 5000；接口会继续递增 `offset`，直到取完匹配记录。若查询全部状态，`offset` 会分别应用于每个状态。基础信息接口保留 Tushare 原字段；股票/商品分类见 `etf_ingest.py` 输出的 `data/etf_metadata.csv`。

拉取所有已上市及已退市的股票、商品 ETF：

```bash
.venv/bin/python etf_ingest.py --all
```

只拉商品 ETF 可加 `--category commodity`；包括其他类型或待核实品种可用 `--category all`。`--refresh` 会重新下载完整历史，`--output` 可修改输出目录。六位代码也可不带交易所后缀，脚本会从 ETF 名录中解析。

结果存放于 `data/etf_metadata.csv` 和 `data/daily/<ETF代码>.csv`。元数据记录 `etf_category`（`stock`、`commodity`、`other`、`unknown`）及 `classification_basis`。行情文件包含原始 Tushare 日线字段和 ETF 分类；若请求失败，另存 `data/errors.csv`。

分类优先采用 `fund_basic` 的投资类型；仅在该字段无法判断时，才根据明确的商品名称或股票名称判断。`etf_basic.etf_type` 仅表示境内/QDII 投资通道，不用于股票/商品分类。`unknown` 应人工核实。

本功能拉取的是 ETF **交易行情**，并非 ETF 基金真实持仓或每日申赎篮子。Tushare 文档标明 `fund_daily` 单次最多 5000 行；ETF 名录与日线接口需要相应积分权限。实际最早可得日期以 Tushare 返回数据为准。
