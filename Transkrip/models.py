"""
models.py — Definisi semua tabel database menggunakan SQLAlchemy ORM.

Setiap class = satu tabel di database.
SQLAlchemy akan otomatis buat tabel ini saat aplikasi pertama kali jalan.
"""

from sqlalchemy import Column, Integer, String, Float, ForeignKey, Enum, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
import enum

# Base adalah "induk" dari semua model.
# Semua class tabel harus inherit dari Base ini.
Base = declarative_base()


# ─────────────────────────────────────────────────────────────
# ENUM: Status tiap mata kuliah (Nilai), bukan status KRS
# ─────────────────────────────────────────────────────────────
class StatusNilai(str, enum.Enum):
    BELUM_TERNILAI = "belum_ternilai"   # Belum semua komponen diisi dosen
    SUDAH_TERNILAI = "sudah_ternilai"   # UTS, UAS, Tes1, Tes2 sudah semua diisi


# ─────────────────────────────────────────────────────────────
# KRS (Kartu Rencana Studi)
# Dibuat saat PRS mahasiswa disetujui (di-push dari service PRS).
# 1 KRS = 1 PRS yang sudah tervalidasi.
# 1 KRS bisa punya banyak Nilai (satu per matkul yang diambil).
# ─────────────────────────────────────────────────────────────
class KRS(Base):
    __tablename__ = "krs"

    id_krs       = Column(Integer, primary_key=True, autoincrement=True)
    id_mahasiswa = Column(Integer, nullable=False)
    semester     = Column(String(10), nullable=False)
    tahun_ajaran = Column(String(10), nullable=False)

    __table_args__ = (
        UniqueConstraint("id_mahasiswa", "semester", "tahun_ajaran", name="unique_krs_mahasiswa_semester"),
    )

# ─────────────────────────────────────────────────────────────
# NILAI
# Satu record Nilai = satu mata kuliah dalam satu KRS.
# ─────────────────────────────────────────────────────────────
class Nilai(Base):
    __tablename__ = "nilai"

    id_nilai    = Column(Integer, primary_key=True, autoincrement=True)
    id_krs      = Column(Integer, ForeignKey("krs.id_krs"), nullable=False)
    id_matkul   = Column(Integer, nullable=False)   # Referensi ke Master service
    id_kelas    = Column(Integer, nullable=False)   # Referensi ke Penawaran Kelas service

    nilai_uts   = Column(Float, nullable=True)
    nilai_uas   = Column(Float, nullable=True)
    nilai_tes1  = Column(Float, nullable=True)
    nilai_tes2  = Column(Float, nullable=True)
    nilai_akhir = Column(Float, nullable=True)
    nilai_huruf = Column(String(2), nullable=True)

    status = Column(
        Enum(StatusNilai),
        default=StatusNilai.BELUM_TERNILAI,
        nullable=False
    )


# ─────────────────────────────────────────────────────────────
# KHS (Kartu Hasil Studi)
# ─────────────────────────────────────────────────────────────
class KHS(Base):
    __tablename__ = "khs"

    id_khs       = Column(Integer, primary_key=True, autoincrement=True)
    id_krs       = Column(Integer, ForeignKey("krs.id_krs"), nullable=False, unique=True)
    semester     = Column(String(10), nullable=False)
    tahun_ajaran = Column(String(10), nullable=False)
    ips          = Column(Float, default=0.0)


# ─────────────────────────────────────────────────────────────
# KHS_DETAIL
# ─────────────────────────────────────────────────────────────
class KHSDetail(Base):
    __tablename__ = "khs_detail"

    id_khs_detail = Column(Integer, primary_key=True, autoincrement=True)
    id_khs        = Column(Integer, ForeignKey("khs.id_khs"), nullable=False)
    id_nilai      = Column(Integer, ForeignKey("nilai.id_nilai"), nullable=False)
    sks           = Column(Integer, nullable=False)
    nilai_huruf   = Column(String(2))
    nilai_akhir   = Column(Float)


# ─────────────────────────────────────────────────────────────
# TRANSKRIP
# ─────────────────────────────────────────────────────────────
class Transkrip(Base):
    __tablename__ = "transkrip"

    id_transkrip = Column(Integer, primary_key=True, autoincrement=True)
    id_mahasiswa = Column(Integer, nullable=False, unique=True)
    total_sks    = Column(Integer, default=0)
    ipk          = Column(Float, default=0.0)


# ─────────────────────────────────────────────────────────────
# DETAIL_TRANSKRIP
# ─────────────────────────────────────────────────────────────
class DetailTranskrip(Base):
    __tablename__ = "detail_transkrip"

    id_detail_transkrip = Column(Integer, primary_key=True, autoincrement=True)
    id_transkrip        = Column(Integer, ForeignKey("transkrip.id_transkrip"), nullable=False)
    id_nilai            = Column(Integer, ForeignKey("nilai.id_nilai"), nullable=False, unique=True)
    id_matkul           = Column(Integer, nullable=False)
    nama_matkul         = Column(String(100), nullable=False)
    semester            = Column(String(10), nullable=False)
    tahun_ajaran        = Column(String(10), nullable=False)
    sks                 = Column(Integer)
    nilai_huruf         = Column(String(2))
    nilai_akhir         = Column(Float)
