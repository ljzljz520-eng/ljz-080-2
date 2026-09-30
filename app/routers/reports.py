"""月度报表（JSON / CSV）。"""
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..schemas import MonthReport
from ..services import reports

router = APIRouter(prefix="/api/reports", tags=["报表"])


@router.get("/monthly", response_model=MonthReport)
def monthly(month: str = "2026-09", db: Session = Depends(get_db)):
    return reports.monthly_report(db, month)


@router.get("/monthly.csv")
def monthly_csv(month: str = "2026-09", db: Session = Depends(get_db)):
    csv_text = reports.monthly_report_csv(db, month)
    return Response(
        content="﻿" + csv_text,  # BOM 方便 Excel
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="energy-{month}.csv"'},
    )
