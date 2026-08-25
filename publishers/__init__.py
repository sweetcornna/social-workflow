"""发布执行层。

- ``base.py``：P0 冻结的发布契约（DTO / 异常分类 / Publisher ABC / FakePublisher）
- ``registry.py``：按 platform 取实现
- ``wechat_mp/`` ``xhs/`` ``douyin/``：各平台实现，分别在 P1 / P2 / P3 填充
"""
