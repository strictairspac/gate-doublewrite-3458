from fastapi import APIRouter, Depends

from src.config.resources import engine
from src.controllers.access import identity
from src.services import handovers, runs

router = APIRouter(tags=["Handovers"])


@router.get("/handovers", dependencies=[Depends(identity)])
def list_handovers(run_id: int | None = None):
    """交班记录。

    * 不带参数：当前每页实际交给下一班的日志页与代次；
    * ``?run_id=N``：某个批次每页的交班记录（同批重试不新增记录）。
    """
    with engine.connect() as db:
        if run_id is not None:
            runs.get(db, run_id)
            return handovers.list_for_run(db, run_id)
        return handovers.list_current(db)


@router.get("/handovers/current", dependencies=[Depends(identity)])
def current_handovers():
    with engine.connect() as db:
        return handovers.list_current(db)
