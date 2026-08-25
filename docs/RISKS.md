# 遗留风险登记册（未公开）

本仓库其他文件里对 `docs/RISKS.md §N` 的交叉引用指向这份文件。**内容没有随开源一起发布。**

原因：那份登记册是一台**在跑的**部署的运维安全档案。它逐条写着该实例的公网地址、
主机名、同租户环境、已加固与**尚未加固**的位置、凭据轮换清单，以及绕过既有闸门的
具体路径。把它公开等于把一份现役系统的攻击手册连同仓库主人的账号名一起交出去——
条目本身的技术结论可以公开，条目附带的坐标不行，而两者在原文里是逐句交织的，
删不干净。

被登记册引用而**确实公开**的部分，都已经落在代码与文档里：

| 风险条目落到哪 | 位置 |
|---|---|
| 端口只绑 loopback（不发布到 `0.0.0.0`） | `docker-compose.yml`、`scripts/gen_xhs_sidecars.py`、`tests/test_port_bindings.py` |
| `SW_UI_TOKEN` 应用层鉴权 | `core/api/`、`scripts/ops/ui_token.sh`、`scripts/ops/env_set.sh` |
| R1 人工确认闸门的硬门禁 | `core/confirm.py`、`scripts/ops/update.sh`、`scripts/ops/restart.sh`、`scripts/ops/verify.sh` |
| 生成 Agent 零工具（提示注入面） | `configs/dsh/cordis.yml`、`tests/generation/test_llm_dsh_live.py` |
| 三方组件 License 与素材合规 | `docs/THIRD_PARTY.md` |
| 平台规则口径（日限、发布权限） | `docs/POLICY.md` |

想复用这套运维姿势的人看 `scripts/ops/README.md` 与 `docs/OPS.md` 就够，
不需要那份实例档案。
