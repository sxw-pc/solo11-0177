# 预售订单交付权益采证台

为离线预售履约演示准备的证据采录工程。三套账分别处理商家承诺与货批、消费者付款与名额、承运交付与退款补偿。数据全部是虚构 JSON；金额使用整数分，日期使用 ISO 日。

## 单命令运行

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m preorderproof demo --db demo.sqlite
python -m unittest discover -s tests -v
```

`demo` 依次导入三套正常夹具、退回复检与三套反例夹具，输出每次导入后的业务状态、当前裁定、历史裁定和原始证据重放审计。输出包含 `imported_module`，应指向当前仓库。

## 各子系统入口

```powershell
python -m preorderproof init --db run.sqlite
python -m preorderproof offer-import --db run.sqlite --file fixtures/offer-ok.json
python -m preorderproof payment-import --db run.sqlite --file fixtures/payment-ok.json
python -m preorderproof delivery-import --db run.sqlite --file fixtures/delivery-ok.json
python -m preorderproof report --db run.sqlite
python -m preorderproof audit --db run.sqlite
```

对应反例是 `fixtures/offer-bad.json`、`fixtures/payment-bad.json` 和 `fixtures/delivery-bad.json`。`offer-bad` 同时展示超宽限改期及同版异量承诺；`payment-bad` 展示消费者同意、尾款逾期和多付款；`delivery-bad` 展示迟到退回及错货批。

## 业务与数据

- `source_events` 保存原始事件、事件日、哈希与所属子系统。相同事件号同内容重放幂等，异内容在事务内拒绝，之前的合法记录不会被部分覆盖。
- `offers`、`batches` 决定承诺版本冲突、货批质量资格及当前可分配容量。质检封存或承诺撤回会改变已付款订单的当前资格。
- `restocks` 记录对实际退回件的独立复检。只有能从已签收包裹证明退回、货批仍合格且在承诺期内的数量才回流可售容量；复检事件也会改变原订单退款义务。迟到或未经复检的退回不会凭单据自动产生库存。
- `orders`、`payments`、`consents` 决定定金占位、尾款时窗、改期同意和多付款债权。超量申请按定金发生日排位，不凭导入顺序获胜；改期同意人必须是订单消费者。
- `parcels`、`receipts` 决定已付订单的承运与签收数量、退回、欠交件数、退款和补偿。错误货批、其他消费者代签或商家单方自报不能兑现交付。
- `current_decisions` 与 `decision_history` 保存当前及每次变化后的业务结果，保留过去的可见结论。`audit` 从原始事件重建业务实体并核对当前裁定。

三条跨账路径在演示中出现：货批/承诺变化改变付款名额，付款与改期同意改变包裹合法性，退回使交付义务与承诺可用量的当前结果重算。迟到证据按事件日参与计算；报告列出引用的业务身份、版次与数量。

## 边界

系统是离线合成证据样例，没有真实支付、物流或身份核验。当前日以已导入事件中最大 `event_at` 为准；演示的责任时间因此可重复。跨商家组合订单、拒付仲裁、冷链索赔和跨境税费不在本版范围。完整源码显著小于建议的 2500–4500 行，题面难度空间仍需用独立业务题实际验证。
