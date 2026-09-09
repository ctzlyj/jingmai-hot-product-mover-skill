# 安装与配置

## 环境要求

- Windows 10/11。
- Python 3.13 用于主流程；Python 3.12 用于读取当前 `jdo-cli` 打包运行时并提交大请求体。
- 已安装并完成 OAuth 登录的 `jdo-cli`。
- Python 包见根目录 `requirements.txt`。
- 可选：`agent-browser`，仅在公开视频地址接口受限时用于浏览器回退。

## 安装依赖

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

若 Python 3.12 不在默认位置，设置：

```powershell
$env:JDO_PYTHON312 = "C:\path\to\python312\python.exe"
```

若 `jdo-cli` 不在 `~/.jdo-cli/bin/jdo-cli.exe`，设置：

```powershell
$env:JDO_CLI_PATH = "C:\path\to\jdo-cli.exe"
```

## 店铺白名单

复制配置：

```powershell
Copy-Item config\store.example.json config\store.local.json
```

填写当前允许操作的店铺：

```json
{
  "store": {
    "profile": "your-store-profile",
    "venderId": 123456789,
    "shopName": "示例店铺"
  },
  "business": {
    "freightTemplateId": 987654321,
    "freightTemplateLabel": "示例运费模板"
  }
}
```

`profile` 使用店铺专属名称且不得为 `default`。`venderId`、店铺名称和运费模板必须从当前店铺真实查询后填写。脚本会核对命令 `--profile` 与配置，并在每次写操作前重新查询店铺信息；`profile`、`venderId` 或 `shopName` 任一不一致立即停止。

`store.local.json` 已被 Git 忽略，禁止提交到公开仓库。

## 登录要求

必须使用用户明确指定、可确认归属的店铺浏览器配置完成 `jdo-cli login --profile your-store-profile`。不要使用未知的系统默认浏览器登录态，也不要把任何店铺登录到 `default` Profile。登录后用相同 Profile 运行只读店铺信息查询，确认返回店铺与本地白名单一致。

本 Skill 不保存 Token、Cookie 或密钥。鉴权、签名和凭据读取始终由官方 `jdo-cli` 完成。

## 刷新实时类目参考

类目 Schema、品牌权限和图片空间均可能变化，首次使用和出现字段变化时必须刷新：

```powershell
.\.venv\Scripts\python.exe scripts\jdo_listing_mvp.py refresh-reference `
  --profile your-store-profile `
  --category-id 35381 `
  --category-name "现代装饰画" `
  --output config\modern-decorative-painting.reference.json
```

该文件包含店铺图片空间 ID 和实时 Schema，已被 Git 忽略，不得公开。

## 安装为 Codex Skill

将仓库目录放入：

```text
%USERPROFILE%\.codex\skills\jd-jingmai-hot-product-mover
```

也可使用 Codex 的 Skill 安装能力从 GitHub 仓库安装。安装后重新打开任务或让 Codex 重新扫描 Skills。

## 发布前自检

1. `jdo-cli status --profile your-store-profile` 显示 OAuth Token 已保存。
2. `store.local.json` 不是示例值，且配置 Profile 与命令 Profile 一致。
3. 实时 reference 文件属于当前登录店铺与目标类目。
4. 运费模板仍存在且适用于目标商品。
5. 测试通过。
6. 用户已明确授权正式发布。
