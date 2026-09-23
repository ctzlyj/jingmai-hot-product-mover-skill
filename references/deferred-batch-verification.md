# 先提交整批，再统一回查

适用于相互独立的多商品批次，包括个人版 `pdd_ab_image_publish.py` 的 A/B 批次、`pdd_repaired_supplement.py` 的当前表补发批次，以及用户明确要求“全部提交后再统一检查”的普通 URL/淘宝搬运批次。沿用当前批次授权；升级不授权续跑历史商品。

## 行为与边界

- 原先发布函数把提交和等待验收放在一起，每件都会阻塞后续商品；批次通用异常处理还会把提交结果不明算成普通失败。A/B 和补发入口复用 `deferred_publish_batch.py`；其他批次适配器也应按同一状态约定把提交和验收拆开。
- 第一阶段依次提交当前选择范围，不等每件传播或审核。保留计划摘要、源表/图片哈希、店铺、A基线和全部现有价格检查；创建前保存 `SUBMITTING`，响应中的商品ID与申请ID分别立即保存，并保留完整响应。一个落盘位置异常时仍尝试保存另一份证据，然后停止写入。
- A/B普通失败继续须有当前跳过授权并传 `--skip-failures`；补发沿用已授权的失败跳过行为。结果不明不当成确定失败，不重新上传/发品；其余独立商品继续。没有跳过授权时保留A/B遇错停止行为。
- 第二阶段在全部可提交商品完成第一阶段后，对已提交、已存在及待核实项统一独立回读，继续使用原有完整验收器：商家原码、逐SKU价格/规格、图片、状态、库存专用接口，以及A/B原链接保护。没有降低验证标准，也不修改商家原码、旧链接或营销活动。
- 已有商品ID只读原商品。无商品ID先读同目录回执；只有申请ID时查原申请，不能把申请ID当商品ID。必要时按唯一技术货号查列表并直查详情验证归属，再完整验收。列表没找到、仍待传播或接口暂不可用都保留待核实；绝不据此再次新增。
- 错店、鉴权/权限失效、本地记录无法可靠保存、无法解释的程序异常立即停止；连续三次接口/传输异常停止当前批次。停止前已提交的项保留待核实，可恢复只回查，不清空状态、不换目录绕过幂等。

## 恢复与交付

原 `publish` 命令及参数继续适用，默认采用两阶段批处理。直接调用单件发布函数仍默认立即验收，批次通过 `defer_verification=True` 延后验收。

只回查同一批次，不需要新的发品口令，不会调用上传、合规提交或创建接口：

```powershell
python scripts/pdd_ab_image_publish.py verify --output <原AB目录> --store-config <店铺配置> --profile <独立Profile> --reference <类目reference>
python scripts/pdd_repaired_supplement.py verify --output <原补发目录> --store-config <店铺配置> --profile <独立Profile> --reference <类目reference>
```

`verify` 仍校验店铺，只访问原计划范围并更新本地回查记录。没有提交证据的项保持未执行，不偷偷发品。保留原商品状态、申请ID、回执、请求体和摘要。

当前总账为 `progress.json`，逐件当前结论为 `batch-result.json`；明确失败另存 `failure.json`，旧失败文件保留历史证据，不用它覆盖新的总账。

- `succeeded`：独立验收通过，不是接口返回成功的数量。
- `failed`：明确失败/驳回/跳过，不包含待核实。
- `awaitingVerification`：已提交或结果不明，尚未验收通过。
- `unexecuted`：本轮未执行的范围，包括 `--limit` 外的项。
- `pending = awaitingVerification + unexecuted`；两者均为零且没有全局停止时才 `completed=true`；`ok=true` 还要求没有失败。
- `phase` 区分 `SUBMITTING`、`VERIFYING`、`FINISHED`；`stage` 的 `PARTIAL` 不能写成全量完成，`COMPLETED_WITH_FAILURES` 不能写成全部成功。

## 验证与回退

离线回归 `python -m unittest discover -s tests -p test_deferred_batch.py -v` 覆盖两阶段调用顺序、待核实计数、无重复提交、只回查命令、回执恢复、商品/申请ID分离、已驳回保留、权限故障及连续接口异常、确认摘要和草稿变更门禁。再运行全套测试与隔离干净分发测试。模拟通过不等于当前线上登录、平台审核或真实业务验收通过。

升级只修改个人版源码，不覆盖业务工作区或旧验证副本。回退只撤销本次代码/文档改动，保留批次状态和全部回执；在结果不明时先只回查，不能用旧版把待核实项当成失败重发。
