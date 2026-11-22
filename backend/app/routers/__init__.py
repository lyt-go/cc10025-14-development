"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource, retention

# 保留期治理路由含 /api/retention/* 与 /api/reclaim_job/{id}/*，
# 必须排在动态资源路由之前注册，否则会被 /api/{module} 抢先匹配。
ROUTERS = [retention, resource]
