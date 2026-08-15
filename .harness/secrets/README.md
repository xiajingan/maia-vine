# .harness/secrets/

本地运行时部署密钥目录；唯一真源为 `.harness/secrets/<env>.sh`。

## 约定

- 首次运行 `python3 scripts/promote_prep.py <env>` 时，若 `<env>.sh` 不存在，会自动生成模板。
- 在本地填写真实值后，重跑 `promote-prep` / `deploy.py preflight-secrets --env <env>`。
- 本目录默认不入库；不要提交真实密钥。

## 常见文件

- `test.sh`
- `prod.sh`
