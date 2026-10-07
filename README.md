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

## 在市 ETF 的日线、复权因子与每日篮子

`etf_market_ingest.py` 使用 `data/etf_basic.csv` 中 `list_status=L` 的 ETF。先查看本次任务范围（无需 Token）：

```bash
python etf_market_ingest.py --all --plan
```

先用沪、深各一只 ETF 试运行：

```bash
python etf_market_ingest.py --codes 510300.SH 159915.SZ --start-date 20260101 --basket-start 20260901
```

批量拉取所有当前在市 ETF：

```bash
python etf_market_ingest.py --all
```

行情 `fund_daily` 和复权因子 `fund_adj` 默认从每只 ETF 的上市日开始请求，并在达到接口单次行数上限时拆分日期；再次运行会补文件前后的日期。篮子组合按交易所分别调用 `etf_sh_cons`、`etf_sz_cons`，未指定 `--start-date` 时默认只拉最近 30 个自然日，按 ETF 和月份保存，可续跑。指定 `--start-date` 时，日线、复权因子和篮子使用同一起点；`--basket-start` 可以单独覆盖篮子起点，`--full-basket-history` 从每只 ETF 上市日回溯篮子。全历史篮子会产生大量请求和文件，建议先用 `--codes` 验证权限与返回字段。`--datasets daily adj basket` 可选择数据集，`--end-date` 限定共同截止日，`--refresh` 重新获取选定区间。

拉取全部在市 ETF 自 2024 年 1 月 1 日起的日线、复权因子和每日一揽子：

```bash
python etf_market_ingest.py --all --datasets daily adj basket --start-date 20240101 --refresh
```

三个数据集的请求起点均为 `20240101` 与每只 ETF 上市日中较晚的一天，截止日均为运行当天；可用 `--end-date YYYYMMDD` 改为同一指定日期。上市前和休市日没有行情。上面的 `--refresh` 会重新请求整个日期范围，以核实已有文件中间是否缺少交易日。完成后去掉 `--refresh` 再运行，脚本会按各数据集的已有记录续拉。`+0 rows` 表示本次没有新增行，例如已拉到最近交易日且后续日期休市。

输出目录为 `data/etf_market/`：`daily/<代码>.csv`、`adj_factor/<代码>.csv`、`pcf/SH/<代码>/<年月>.csv`、`pcf/SZ/<代码>/<年月>.csv`，以及 `errors.csv`。篮子包含申赎现金替代等信息，是交易所盘前披露的 PCF，不能直接当作基金实际持仓。当前名录中若缺少 `list_date`，脚本会从 `setup_date` 开始查询；同一 ETF 的 `.OF` 和交易所代码会归并到交易所代码。

官方接口及权限：[ETF 日线](https://tushare.pro/document/2?doc_id=127)、[基金复权因子](https://tushare.pro/document/2?doc_id=199)、[沪市篮子](https://tushare.pro/document/2?doc_id=471)、[深市篮子](https://tushare.pro/document/2?doc_id=472)。篮子接口需要 8000 积分；具体可取日期以接口返回为准。

## 根据 ETF 篮子汇总成分股，再拉取股票信息与历史日线

`etf_constituent_ingest.py` 读取已有的 `data/etf_market/pcf/SH/` 和 `SZ/` 月度 CSV，保留每日 ETF、成分代码、数量、现金替代标志等关系；再汇总去重的股票代码，分别调用 A 股 `stock_basic` / `daily`、港股 `hk_basic` / `hk_daily`、美股 `us_basic` / `us_daily`。现金项和无法可靠映射市场的代码单独列出，不发送股票行情请求。港股代码会补齐至五位，美股代码以 `us_basic` 的返回结果确认。某只股票出现在多只 ETF 时，基础信息和日线只按去重后的代码拉取一次。

已有 PCF 文件后，可先只汇总本地成分股（不需要 Token）：

```bash
.venv/bin/python etf_constituent_ingest.py --aggregate-only
```

准备好相应接口权限和 `TUSHARE_TOKEN` 后，运行完整拉取：

```bash
.venv/bin/python etf_constituent_ingest.py
```

默认日线范围为每只股票**首次出现在本地 PCF 的日期至运行当天**；实际历史长度还取决于已有 PCF 覆盖和 Tushare 返回的数据。可指定日期，或从股票上市日开始回溯：

```bash
.venv/bin/python etf_constituent_ingest.py --start-date 20260101 --end-date 20260930
.venv/bin/python etf_constituent_ingest.py --from-listing
.venv/bin/python etf_constituent_ingest.py --markets HK US --max-stocks 20
```

`--start-date` 与 `--from-listing` 不可同时使用。`--markets` 只限制后续股票请求，不限制本地汇总。`--max-stocks` 适合先试少量去重股票。再次运行会复用基础信息文件并向前或向后补齐日线；`--refresh-basic`、`--refresh-daily` 可重新请求。`--pcf-root` 和 `--output` 可修改输入、输出路径。

输出位于 `data/constituents/`：

- `pcf_membership.sqlite`：完整的本地每日 ETF 与成分关系，可按 `etf_code`、`trade_date` 查询。PCF 文件有改动时会自动重新导入。
- `constituents.csv`：去重股票代码、首次/最后出现日期、涉及 ETF 数量和 PCF 行数。
- `basic_catalog.csv` 及 `basic/<市场>/<代码>.csv`：股票基础信息。`daily/<市场>/<代码>.csv`：对应的原始历史日线，包含 Tushare 提供的每日涨跌幅字段；美股字段名为 `pct_change`，A/港股为 `pct_chg`。
- `non_stock_pcf_codes.csv`：现金项、无法识别的代码；`issues.csv`：无法匹配股票基础信息或有美股代码复用风险的记录；`errors.csv`：接口失败记录。

该数据源是交易所披露的**每日申赎篮子 PCF**，不是基金实际持仓。部分 QDII ETF 的 PCF 可能只含现金或不含美股代码，因此脚本只能拉取**篮子中可识别且能在对应股票基础接口匹配的**美港股。A、港、美日线均保留原始未复权行情；港股和美股日线需要分别具备相应权限。接口文档：[A 股基础信息](https://tushare.pro/document/1?doc_id=25)、[A 股日线](https://tushare.pro/document/2?doc_id=27)、[港股基础信息](https://tushare.pro/document/2?doc_id=191)、[港股日线](https://tushare.pro/document/2?doc_id=192)、[美股基础信息](https://tushare.pro/document/2?doc_id=252)、[美股日线](https://tushare.pro/document/2?doc_id=254)。

### 一次完成 2023-01-01 至 2026-10-01 的 ETF 成分股日线

`etf_stock_history_ingest.py` 是统一入口。它先按 ETF 名录补齐这段时间的沪深 ETF 每日 PCF，再在该日期范围内汇总去重的成分股，最后分别下载 A 股、港股和美股基础信息及 **2023-01-01 至 2026-10-01** 的未复权日线。默认包括名录中目前在市和已退市的 ETF；`--listed-only` 只选目前在市的 ETF。单只 ETF 的 PCF 请求从其上市日与起始日期中较晚者开始；个股日线仍按指定完整日期范围请求，由接口决定上市前有无数据。

激活已安装 `requirements.txt` 的 Python 环境并配置 Tushare Token 后，在项目根目录手动运行：

```bash
python etf_stock_history_ingest.py
```

可先手动查看 ETF 数量而不发起接口请求：

```bash
python etf_stock_history_ingest.py --plan
```

脚本支持 `--start-date`、`--end-date`、`--catalog`、`--pcf-output`、`--output`、`--markets`、`--max-stocks` 和 `--interval`。重新请求已有月份 PCF 用 `--refresh-pcf`；重新请求已有基础信息或日线用 `--refresh-basic`、`--refresh-daily`。正常重跑会利用 PCF 覆盖记录与个股文件续拉。输出包括 `data/constituents/pcf_membership.sqlite`、`constituents.csv`、`basic_catalog.csv`、`daily/<市场>/<代码>.csv`、`pcf_errors.csv`、`issues.csv` 和 `errors.csv`。如果任一 ETF 的 PCF 请求失败，脚本会写入 `pcf_errors.csv` 并在股票汇总前停止，避免误将不完整的篮子当作全部成分股。

当前本地 PCF 文件最早为 **2024 年 1 月**；脚本运行时需要相应 Tushare 权限才能补齐 2023 年。数据源没有提供的 ETF/交易日/成分代码不会凭空生成；输出应以实际接口返回为准。
