"""Authenticated single-owner portfolio snapshot; explicit save/restore only."""
import json
from typing import Literal, Annotated
from pydantic import BaseModel, Field, ConfigDict, model_validator
from fastapi import APIRouter, Depends
from .admin import require_admin, require_write
from .db import Session, PortfolioSnapshot, now, DEMO

router=APIRouter(prefix='/api/portfolio',tags=['portfolio'])
Positive=Annotated[float,Field(gt=0,le=1e12,allow_inf_nan=False)]
Nonnegative=Annotated[float,Field(ge=0,le=1e12,allow_inf_nan=False)]
Percent=Annotated[float,Field(ge=0,le=100,allow_inf_nan=False)]
Return=Annotated[float,Field(ge=-100,le=10000,allow_inf_nan=False)]
class Holding(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    symbol:str=Field(min_length=1,max_length=80)
    currency:Literal['KRW','USD','GBP']
    eps:Positive
    price:Positive
    qty:Positive
    pers:list[Positive]=Field(min_length=3,max_length=3)
    @model_validator(mode='after')
    def per_order(self):
        if not self.pers[0]<=self.pers[1]<=self.pers[2]: raise ValueError('PER 순서 오류')
        return self
class Portfolio(BaseModel):
    model_config=ConfigDict(extra='forbid')
    holdings:list[Holding]=Field(max_length=500)
    stock:Nonnegative
    bond:Nonnegative
    cash:Nonnegative
    ai:Percent
    ai_return:Return
    other_return:Return
    bond_return:Return
    mode:Literal['model','manual']
    target:list[Percent]=Field(min_length=3,max_length=3)
    @model_validator(mode='after')
    def weight_sum(self):
        if self.mode=='manual' and abs(sum(self.target)-100)>0.0001: raise ValueError('비중 합계 오류')
        return self

def output(row):
    if not row:return {'demo':DEMO,'saved':False,'snapshot':None,'updated':None}
    return {'demo':DEMO,'saved':True,'snapshot':json.loads(row.payload),'updated':row.updated.isoformat()+'Z'}

@router.get('',dependencies=[Depends(require_admin)])
def get_portfolio():
    with Session() as db:return output(db.get(PortfolioSnapshot,1))

@router.put('',dependencies=[Depends(require_write)])
def put_portfolio(req:Portfolio):
    with Session.begin() as db:
        row=db.merge(PortfolioSnapshot(id=1,payload=req.model_dump_json(),updated=now()))
        return output(row)
