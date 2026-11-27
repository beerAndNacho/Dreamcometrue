import os
from pathlib import Path
from datetime import date, datetime, timezone
from dotenv import load_dotenv
from sqlalchemy import create_engine, String, Float, Integer, Date, DateTime, Text, select, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

load_dotenv()
DEMO = os.getenv('DEMO_MODE','0') == '1'
url = os.getenv('DATABASE_URL','sqlite:///./data/invest.db')
if DEMO:
    url = os.getenv('DEMO_DATABASE_URL','sqlite:///./data/demo.db')
    if not url.startswith('sqlite:///'): raise RuntimeError('DEMO_DATABASE_URL must be SQLite')
if url.startswith(('postgresql://','postgres://')):
    url = 'postgresql+psycopg://' + url.split('://',1)[1]
if url.startswith('sqlite:///') and ':memory:' not in url:
    Path(url.removeprefix('sqlite:///')).parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(url, pool_pre_ping=True, connect_args={'check_same_thread':False} if url.startswith('sqlite') else {})
if url.startswith('sqlite'):
    @event.listens_for(engine, 'connect')
    def sqlite_options(conn, _):
        cur=conn.cursor()
        cur.execute('PRAGMA busy_timeout=10000')
        cur.execute('PRAGMA journal_mode=WAL')
        cur.close()
Session = sessionmaker(bind=engine, expire_on_commit=False)

def now(): return datetime.now(timezone.utc).replace(tzinfo=None)
class Base(DeclarativeBase): pass
class Obs(Base):
    __tablename__='observations'
    series: Mapped[str]=mapped_column(String(50),primary_key=True)
    period: Mapped[date]=mapped_column(Date,primary_key=True)
    value: Mapped[float]=mapped_column(Float)
    fetched: Mapped[datetime]=mapped_column(DateTime)
class Revision(Base):
    __tablename__='revisions'
    id: Mapped[int]=mapped_column(Integer,primary_key=True,autoincrement=True)
    series: Mapped[str]=mapped_column(String(50),index=True)
    period: Mapped[date]=mapped_column(Date)
    value: Mapped[float]=mapped_column(Float)
    seen: Mapped[datetime]=mapped_column(DateTime)
class Status(Base):
    __tablename__='collection_status'
    series: Mapped[str]=mapped_column(String(50),primary_key=True)
    state: Mapped[str]=mapped_column(String(20))
    message: Mapped[str]=mapped_column(Text)
    attempted: Mapped[datetime]=mapped_column(DateTime)
    success: Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
class PortfolioSnapshot(Base):
    __tablename__='portfolio_snapshots'
    id: Mapped[int]=mapped_column(Integer,primary_key=True)
    payload: Mapped[str]=mapped_column(Text)
    updated: Mapped[datetime]=mapped_column(DateTime)

class DailySnapshot(Base):
    __tablename__='daily_market_snapshots'
    day: Mapped[date]=mapped_column(Date,primary_key=True)
    payload: Mapped[str]=mapped_column(Text)
    updated: Mapped[datetime]=mapped_column(DateTime)

class SeriesSource(Base):
    """Non-secret identity of the selected source table/item for configurable series."""
    __tablename__='series_sources'
    series: Mapped[str]=mapped_column(String(50),primary_key=True)
    fingerprint: Mapped[str]=mapped_column(String(64))

class Run(Base):
    __tablename__='runs'
    id: Mapped[int]=mapped_column(Integer,primary_key=True,autoincrement=True)
    started: Mapped[datetime]=mapped_column(DateTime)
    finished: Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    state: Mapped[str]=mapped_column(String(20))

def init(): Base.metadata.create_all(engine)

def write_series(db, sid, points):
    if not points: return 0
    existing={o.period:o for o in db.scalars(select(Obs).where(Obs.series==sid,Obs.period>=min(points),Obs.period<=max(points)))}
    stamp=now(); changed=0
    for period,value in points.items():
        old=existing.get(period)
        if old is None or old.value!=value:
            db.add(Revision(series=sid,period=period,value=value,seen=stamp))
            if old is None: db.add(Obs(series=sid,period=period,value=value,fetched=stamp))
            else: old.value=value; old.fetched=stamp
            changed+=1
    return changed

def history(db,sid,limit=None):
    # Select scalar columns rather than constructing ORM objects for read-only charts.
    q=select(Obs.period,Obs.value).where(Obs.series==sid)
    if limit is not None:
        return list(reversed(db.execute(q.order_by(Obs.period.desc()).limit(limit)).tuples().all()))
    return db.execute(q.order_by(Obs.period)).tuples().all()

class MarketCache(Base):
    __tablename__='kr_market_cache'
    key: Mapped[str]=mapped_column(String(64),primary_key=True)
    payload: Mapped[str]=mapped_column(Text)
    state: Mapped[str]=mapped_column(String(20))
    message: Mapped[str]=mapped_column(Text)
    updated: Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    attempted: Mapped[datetime]=mapped_column(DateTime)

class MarketWatch(Base):
    __tablename__='kr_market_watch'
    code: Mapped[str]=mapped_column(String(6),primary_key=True)
    name: Mapped[str]=mapped_column(String(80))
    corp_code: Mapped[str]=mapped_column(String(8),default='')
