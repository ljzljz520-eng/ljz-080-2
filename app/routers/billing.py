"""离店结账与能耗账单。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Bill, Booking
from ..schemas import BillOut
from ..services import billing as billing_svc

router = APIRouter(prefix="/api", tags=["账单"])


@router.post("/bookings/{booking_id}/checkout", response_model=BillOut)
def checkout(booking_id: int, db: Session = Depends(get_db)):
    b = db.get(Booking, booking_id)
    if b is None:
        raise HTTPException(404, "预订不存在")
    bill = billing_svc.checkout(db, booking_id)
    db.commit()
    db.refresh(bill)
    return bill


@router.get("/bills", response_model=list[BillOut])
def list_bills(property_id: int | None = None, db: Session = Depends(get_db)):
    stmt = select(Bill).order_by(Bill.issued_at.desc())
    if property_id:
        stmt = stmt.where(Bill.property_id == property_id)
    return list(db.scalars(stmt))


@router.get("/bookings/{booking_id}/bill", response_model=BillOut)
def booking_bill(booking_id: int, db: Session = Depends(get_db)):
    bill = db.scalar(select(Bill).where(Bill.booking_id == booking_id))
    if bill is None:
        raise HTTPException(404, "账单尚未生成")
    return bill
