# 搬家后逐链接核对

店铺、目标商品和允许的写入范围沿用本次交办。先验两侧 Profile、shopId、venderId 和店名。遇到同源多目标，可用旧批次中的发布回执和线上商品回读追查每条链接，并复核当前商品和规格事实。同名、同图、图片空间里的候选都不能证明绑定成功。

```text
python scripts/jd_product_pipeline.py shop-copy history-census --target-profile TARGET --target-config target.json --history-dir original-run
python scripts/jd_product_pipeline.py shop-copy copy-materials --source-profile SOURCE --source-config source.json --source-product-id 701 --target-profile TARGET --target-config target.json --target-product-id 802 --target-product-id 803 --history-dir original-run
```

指定目标的只读核对会复用本次源快照。加 `--health` 后，多个目标共用一次按店铺身份隔离的健康分页。写入使用 `--confirm FILL_MISSING_COPY_MATERIALS`，每个目标重新读取源和目标事实，只填明确缺失的槽位。目标已有不同内容、平台未回该 SKU、审核中、请求结果未知，都保留具体状态。批量和单链接都返回本机回执路径。用原范围重跑会先核对槽位及旧请求；每个目标的独立锁防止并发重复补绑。

`copy-short-titles` 接受两侧与上例相同的身份参数和一个目标 SPU。采销端从同一安装根的 `jd-material-pop` 读取平台模板；源码分开存放时传 `--materials-root <该 Skill 目录>`。本机回执区分已匹配、来源为空、目标已保留和待补的 SKU。输出工作表供信息分模块在目标店授权下导入并回读，准备工作表时平台写入数为 0。

## 同一问题只修一处

逐规格关联、素材迁移和短标题比对以本仓 `scripts/pop_copy/` 为源，店铺授权和 CLI 由当前宿主提供。初始实现取自商家版 `1c68ab954dc77fff30054f2c0a32273acdcf02d8`。业务变更先在本仓复现，修逻辑并补 `tests/test_copy_materials.py` 或 `tests/pop_copy_contract.py`。用完整 Git 提交和文件哈希同步到两版候选，分别从常规入口运行再发布。需要平台、宿主或真人的验收单独记录。

```text
python -m unittest discover -s tests -p test_copy_materials.py
python tests/pop_copy_contract.py
python tests/pop_copy_contract.py --merchant-root <商家源码目录>
```

入口回归使用临时安装目录、合成的两个店和两个目标商品。用实际产品参数启动进程，官方接口由本地测试程序返回固定回执。两版都应给出一个“源素材为空”和一个“商品关系未核实”，两份独立明细、零写入、整批未完成。
