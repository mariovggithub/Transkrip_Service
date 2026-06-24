"""
service.py — Entry point utama Nameko microservice Transkrip.

PERUBAHAN dari versi sebelumnya:
  - get_transkrip_mahasiswa: mapping field "name" → "nama" dari Master
  - push_semester_ke_krs: sudah menggunakan id_semester (tidak berubah)
"""
from nameko.rpc import rpc, RpcProxy
from nameko_sqlalchemy import DatabaseSession

from .models import (
    Base, KRS, KHS, KHSDetail,
    Nilai, Transkrip, DetailTranskrip, StatusNilai
)
from .utils import (
    hitung_nilai_akhir, nilai_ke_huruf,
    hitung_ips, semua_nilai_lengkap, validasi_komponen
)


class TranskripService:
    name = "transkrip_service"

    db     = DatabaseSession(Base)
    master = RpcProxy("master_service")
    prs    = RpcProxy("prs_service")

    @rpc
    def push_semester_ke_krs(self, id_semester: int):
        """
        Tarik semua peserta tervalidasi dari PRS untuk satu semester,
        lalu buat KRS + Nilai kosong untuk masing-masing mahasiswa.
        """
        if id_semester is None:
            return {"status": "error", "message": "id_semester wajib diisi"}

        semester_resp = self.master.get_semester_by_id(id_semester)
        if semester_resp.get("status") != "success":
            return {"status": "error", "message": f"Semester {id_semester} tidak ditemukan di master_service"}

        semester_data = semester_resp["data"]
        semester_nama = semester_data["name"]
        tahun_ajaran  = str(semester_data["year"])

        prs_resp = self.prs.push_peserta_to_transkrip(id_semester)
        if "error" in prs_resp:
            return {"status": "error", "message": prs_resp["error"]}

        peserta_list = prs_resp.get("peserta", [])

        per_mahasiswa = {}
        for peserta in peserta_list:
            id_mhs = peserta["id_mahasiswa"]
            per_mahasiswa.setdefault(id_mhs, []).append(peserta)

        hasil = {"berhasil": [], "dilewati": []}

        for id_mahasiswa, daftar_matkul in per_mahasiswa.items():
            existing = self.db.query(KRS).filter_by(
                id_mahasiswa=id_mahasiswa, semester=semester_nama, tahun_ajaran=tahun_ajaran
            ).first()
            if existing:
                hasil["dilewati"].append({"id_mahasiswa": id_mahasiswa, "alasan": "KRS sudah ada"})
                continue

            krs = KRS(
                id_mahasiswa=id_mahasiswa,
                semester=semester_nama,
                tahun_ajaran=tahun_ajaran,
            )
            self.db.add(krs)
            self.db.flush()

            for matkul in daftar_matkul:
                nilai = Nilai(
                    id_krs    = krs.id_krs,
                    id_mahasiswa = id_mahasiswa,
                    id_matkul = matkul["id_mata_kuliah"],
                    id_kelas  = matkul["id_kelas"],
                    nilai_uts  = None,
                    nilai_uas  = None,
                    nilai_tes1 = None,
                    nilai_tes2 = None,
                    status     = StatusNilai.BELUM_TERNILAI,
                )
                self.db.add(nilai)

            hasil["berhasil"].append({
                "id_mahasiswa": id_mahasiswa,
                "id_krs": krs.id_krs,
                "jumlah_matkul": len(daftar_matkul)
            })

        self.db.commit()
        return {"status": "ok", **hasil}

    @rpc
    def input_nilai(self, id_nilai: int, komponen: str, nilai: float):
        """
        Dosen mengisi salah satu komponen nilai untuk satu mata kuliah.
        """
        if not validasi_komponen(komponen):
            return {
                "status": "error",
                "message": f"Komponen '{komponen}' tidak valid. Gunakan: uts, uas, tes1, tes2"
            }

        try:
            nilai = float(nilai)
        except (TypeError, ValueError):
            return {"status": "error", "message": "Nilai harus berupa angka"}

        if not (0.0 <= nilai <= 100.0):
            return {"status": "error", "message": "Nilai harus antara 0 dan 100"}

        record = self.db.query(Nilai).filter_by(id_nilai=id_nilai).first()
        if not record:
            return {"status": "error", "message": f"Nilai dengan id {id_nilai} tidak ditemukan"}

        setattr(record, f"nilai_{komponen}", nilai)

        if semua_nilai_lengkap(record.nilai_uts, record.nilai_uas, record.nilai_tes1, record.nilai_tes2):
            akhir        = hitung_nilai_akhir(record.nilai_uts, record.nilai_uas, record.nilai_tes1, record.nilai_tes2)
            huruf, bobot = nilai_ke_huruf(akhir)

            record.nilai_akhir = akhir
            record.nilai_huruf = huruf
            record.status      = StatusNilai.SUDAH_TERNILAI

            krs = self.db.query(KRS).filter_by(id_krs=record.id_krs).first()

            matkul_resp = self.master.get_course_by_id(record.id_matkul)
            matkul_data = matkul_resp.get("data", {}) if matkul_resp.get("status") == "success" else {}
            sks         = matkul_data.get("sks", 0)
            nama_matkul = matkul_data.get("name", f"Matkul #{record.id_matkul}")

            self._update_khs(krs, record, sks)
            self._update_detail_transkrip(krs, record, sks, nama_matkul)
            self._hitung_ulang_ipk(krs.id_mahasiswa)

        self.db.commit()
        return {"status": "ok", "nilai_huruf": record.nilai_huruf}
    
    @rpc
    def input_nilai_kelas(
        self,
        id_kelas: int,
        komponen: str,
        daftar_nilai: list
    ):

        if not validasi_komponen(komponen):
            return {
                "status": "error",
                "message": "Komponen tidak valid"
            }

        berhasil = []
        gagal = []

        for item in daftar_nilai:

            try:

                id_nilai = item["id_nilai"]
                nilai = float(item["nilai"])

                if nilai < 0 or nilai > 100:
                    raise ValueError(
                        "Nilai harus antara 0-100"
                    )

                record = self.db.query(Nilai)\
                    .filter_by(
                        id_nilai=id_nilai,
                        id_kelas=id_kelas
                    )\
                    .first()

                if not record:

                    gagal.append({
                        "id_nilai": id_nilai,
                        "message": "Data tidak ditemukan"
                    })

                    continue

                setattr(
                    record,
                    f"nilai_{komponen}",
                    nilai
                )

                if semua_nilai_lengkap(
                    record.nilai_uts,
                    record.nilai_uas,
                    record.nilai_tes1,
                    record.nilai_tes2
                ):
                    self._proses_nilai_lengkap(
                        record
                    )

                berhasil.append(id_nilai)

            except Exception as e:

                gagal.append({
                    "id_nilai": item.get("id_nilai"),
                    "message": str(e)
                })

        self.db.commit()

        return {
            "status": "ok",
            "jumlah_berhasil": len(berhasil),
            "jumlah_gagal": len(gagal),
            "berhasil": berhasil,
            "gagal": gagal
        }

    def _update_khs(self, krs: KRS, nilai: Nilai, sks: int):
        khs = self.db.query(KHS).filter_by(id_krs=krs.id_krs).first()
        if not khs:
            khs = KHS(
                id_krs       = krs.id_krs,
                semester     = krs.semester,
                tahun_ajaran = krs.tahun_ajaran,
                ips          = 0.0,
            )
            self.db.add(khs)
            self.db.flush()

        existing_detail = self.db.query(KHSDetail).filter_by(id_nilai=nilai.id_nilai).first()
        if not existing_detail:
            khs_detail = KHSDetail(
                id_khs      = khs.id_khs,
                id_nilai    = nilai.id_nilai,
                sks         = sks,
                nilai_huruf = nilai.nilai_huruf,
                nilai_akhir = nilai.nilai_akhir,
            )
            self.db.add(khs_detail)
        else:
            existing_detail.nilai_huruf = nilai.nilai_huruf
            existing_detail.nilai_akhir = nilai.nilai_akhir
            existing_detail.sks         = sks

        self.db.flush()

        semua_detail = self.db.query(KHSDetail).filter_by(id_khs=khs.id_khs).all()
        detail_list  = []
        for d in semua_detail:
            _, bobot = nilai_ke_huruf(d.nilai_akhir)
            detail_list.append({"sks": d.sks, "bobot": bobot})

        khs.ips = hitung_ips(detail_list)

    def _update_detail_transkrip(self, krs: KRS, nilai: Nilai, sks: int, nama_matkul: str):
        transkrip = self.db.query(Transkrip).filter_by(id_mahasiswa=krs.id_mahasiswa).first()
        if not transkrip:
            transkrip = Transkrip(id_mahasiswa=krs.id_mahasiswa)
            self.db.add(transkrip)
            self.db.flush()

        existing = self.db.query(DetailTranskrip).filter_by(id_nilai=nilai.id_nilai).first()
        if not existing:
            detail = DetailTranskrip(
                id_transkrip = transkrip.id_transkrip,
                id_nilai     = nilai.id_nilai,
                id_matkul    = nilai.id_matkul,
                nama_matkul  = nama_matkul,
                semester     = krs.semester,
                tahun_ajaran = krs.tahun_ajaran,
                sks          = sks,
                nilai_huruf  = nilai.nilai_huruf,
                nilai_akhir  = nilai.nilai_akhir,
            )
            self.db.add(detail)
        else:
            existing.nilai_huruf = nilai.nilai_huruf
            existing.nilai_akhir = nilai.nilai_akhir
            existing.sks         = sks

        self.db.flush()

    def _hitung_ulang_ipk(self, id_mahasiswa: int):
        transkrip = self.db.query(Transkrip).filter_by(id_mahasiswa=id_mahasiswa).first()
        if not transkrip:
            return

        semua_detail = self.db.query(DetailTranskrip).filter_by(
            id_transkrip=transkrip.id_transkrip
        ).all()

        detail_list = []
        for d in semua_detail:
            _, bobot = nilai_ke_huruf(d.nilai_akhir)
            detail_list.append({"sks": d.sks, "bobot": bobot})

        transkrip.total_sks = sum(d["sks"] for d in detail_list)
        transkrip.ipk       = hitung_ips(detail_list)

    @rpc
    def get_nilai_by_kelas(self, id_kelas: int):
        nilai_list = self.db.query(Nilai).filter_by(id_kelas=id_kelas).all()
        return [
            {
                "id_nilai":    n.id_nilai,
                "id_krs":      n.id_krs,
                "id_matkul":   n.id_matkul,
                "nilai_uts":   n.nilai_uts,
                "nilai_uas":   n.nilai_uas,
                "nilai_tes1":  n.nilai_tes1,
                "nilai_tes2":  n.nilai_tes2,
                "nilai_akhir": n.nilai_akhir,
                "nilai_huruf": n.nilai_huruf,
                "status":      n.status.value if n.status else None,
            }
            for n in nilai_list
        ]

    @rpc
    def get_mahasiswa_nilai_by_kelas(self, id_kelas: int):

        nilai_list = self.db.query(Nilai)\
            .filter_by(id_kelas=id_kelas)\
            .all()

        hasil = []

        for nilai in nilai_list:

            krs = self.db.query(KRS)\
                .filter_by(id_krs=nilai.id_krs)\
                .first()

            mahasiswa = {
                "id": krs.id_mahasiswa,
                "nama": f"Mahasiswa {krs.id_mahasiswa}"
            }

            try:
                mhs_resp = self.master.get_student_by_id(
                    krs.id_mahasiswa
                )

                if mhs_resp.get("status") == "success":
                    mahasiswa["nama"] = mhs_resp["data"].get(
                        "name",
                        mahasiswa["nama"]
                    )

            except Exception:
                pass

            hasil.append({
                "id_nilai": nilai.id_nilai,
                "id_mahasiswa": krs.id_mahasiswa,
                "nama_mahasiswa": mahasiswa["nama"],

                "nilai_uts": nilai.nilai_uts,
                "nilai_uas": nilai.nilai_uas,
                "nilai_tes1": nilai.nilai_tes1,
                "nilai_tes2": nilai.nilai_tes2,

                "status": nilai.status.value
            })

        return hasil
    
    def _proses_nilai_lengkap(self, record: Nilai):

        akhir = hitung_nilai_akhir(
            record.nilai_uts,
            record.nilai_uas,
            record.nilai_tes1,
            record.nilai_tes2
        )

        huruf, bobot = nilai_ke_huruf(akhir)

        record.nilai_akhir = akhir
        record.nilai_huruf = huruf
        record.status = StatusNilai.SUDAH_TERNILAI

        krs = self.db.query(KRS)\
            .filter_by(id_krs=record.id_krs)\
            .first()

        matkul_resp = self.master.get_course_by_id(
            record.id_matkul
        )

        matkul_data = (
            matkul_resp["data"]
            if matkul_resp.get("status") == "success"
            else {}
        )

        sks = matkul_data.get("sks", 0)

        nama_matkul = matkul_data.get(
            "name",
            f"Matkul {record.id_matkul}"
        )

        self._update_khs(
            krs,
            record,
            sks
        )

        self._update_detail_transkrip(
            krs,
            record,
            sks,
            nama_matkul
        )

        self._hitung_ulang_ipk(
            krs.id_mahasiswa
        )


    @rpc
    def get_khs_by_mahasiswa(self, id_mahasiswa: int, semester: str, tahun_ajaran: str):
        krs_list = self.db.query(KRS).filter_by(
            id_mahasiswa=id_mahasiswa,
            semester=semester,
            tahun_ajaran=tahun_ajaran
        ).all()

        hasil_matkul = []
        ips_semester = 0.0

        for krs in krs_list:
            khs = self.db.query(KHS).filter_by(id_krs=krs.id_krs).first()
            if not khs:
                continue

            ips_semester = khs.ips

            details = self.db.query(KHSDetail).filter_by(id_khs=khs.id_khs).all()
            for d in details:
                nilai = self.db.query(Nilai).filter_by(id_nilai=d.id_nilai).first()
                hasil_matkul.append({
                    "id_nilai":    nilai.id_nilai,
                    "id_matkul":   nilai.id_matkul,
                    "sks":         d.sks,
                    "nilai_uts":   nilai.nilai_uts,
                    "nilai_uas":   nilai.nilai_uas,
                    "nilai_tes1":  nilai.nilai_tes1,
                    "nilai_tes2":  nilai.nilai_tes2,
                    "nilai_akhir": d.nilai_akhir,
                    "nilai_huruf": d.nilai_huruf,
                    "status":      nilai.status.value if nilai.status else None,
                })

        return {
            "id_mahasiswa": id_mahasiswa,
            "semester":     semester,
            "tahun_ajaran": tahun_ajaran,
            "ips":          ips_semester,
            "matkul":       hasil_matkul,
        }

    @rpc
    def get_transkrip_mahasiswa(self, id_mahasiswa: int):
        """
        PERBAIKAN: mapping field "name" dari Master → "nama" untuk konsistensi internal.
        """
        transkrip = self.db.query(Transkrip).filter_by(id_mahasiswa=id_mahasiswa).first()

        if not transkrip:
            return {"status": "error", "message": "Transkrip belum tersedia"}

        try:
            mhs_resp = self.master.get_student_by_id(id_mahasiswa)
            if mhs_resp.get("status") == "success":
                raw = mhs_resp["data"]
                mahasiswa = {
                    "id_mahasiswa": raw.get("id"),
                    "nama":         raw.get("name"),   # Master pakai "name", kita map ke "nama"
                    "nrp":          raw.get("nrp"),
                    "email":        raw.get("email"),
                }
            else:
                mahasiswa = {"id_mahasiswa": id_mahasiswa}
        except Exception:
            mahasiswa = {"id_mahasiswa": id_mahasiswa}

        details = self.db.query(DetailTranskrip).filter_by(
            id_transkrip=transkrip.id_transkrip
        ).order_by(DetailTranskrip.tahun_ajaran, DetailTranskrip.semester).all()

        per_semester = {}
        for d in details:
            key = f"{d.tahun_ajaran}-{d.semester}"
            if key not in per_semester:
                per_semester[key] = {
                    "semester":     d.semester,
                    "tahun_ajaran": d.tahun_ajaran,
                    "matkul":       [],
                }
            per_semester[key]["matkul"].append({
                "nama_matkul": d.nama_matkul,
                "sks":         d.sks,
                "nilai_huruf": d.nilai_huruf,
                "nilai_akhir": d.nilai_akhir,
            })

        return {
            "status":    "ok",
            "mahasiswa": mahasiswa,
            "total_sks": transkrip.total_sks,
            "ipk":       transkrip.ipk,
            "riwayat":   list(per_semester.values()),
        }

    @rpc
    def get_ips_per_semester(self, id_mahasiswa: int):
        krs_list = self.db.query(KRS).filter_by(id_mahasiswa=id_mahasiswa).all()

        riwayat = []
        for krs in krs_list:
            khs = self.db.query(KHS).filter_by(id_krs=krs.id_krs).first()
            if khs:
                riwayat.append({
                    "semester":     khs.semester,
                    "tahun_ajaran": khs.tahun_ajaran,
                    "ips":          khs.ips,
                })

        riwayat.sort(key=lambda x: (x["tahun_ajaran"], x["semester"]))
        return riwayat

    @rpc
    def get_ipk_mahasiswa(self, id_mahasiswa: int):
        transkrip = self.db.query(Transkrip).filter_by(id_mahasiswa=id_mahasiswa).first()
        return {
            "id_mahasiswa": id_mahasiswa,
            "ipk":          transkrip.ipk if transkrip else 0.0,
            "total_sks":    transkrip.total_sks if transkrip else 0,
        }

    @rpc
    def get_krs_by_mahasiswa(self, id_mahasiswa: int):
        krs_list = self.db.query(KRS).filter_by(id_mahasiswa=id_mahasiswa).all()
        return [
            {
                "id_krs":       k.id_krs,
                "id_mahasiswa": k.id_mahasiswa,
                "semester":     k.semester,
                "tahun_ajaran": k.tahun_ajaran,
            }
            for k in krs_list
        ]
    
    @rpc
    def push_prs_ke_krs(self, id_prs: int):
        prs_data = self.prs.get_prs_by_id(id_prs)
        if "error" in prs_data:
            return {"status": "error", "message": prs_data["error"]}
        id_mahasiswa = prs_data["id_mahasiswa"]
        id_semester  = prs_data["id_semester"]
        semester_resp = self.master.get_semester_by_id(id_semester)
        if semester_resp.get("status") != "success":
            return {"status": "error", "message": f"Semester {id_semester} tidak ditemukan"}
        semester_nama = semester_resp["data"]["name"]
        tahun_ajaran  = str(semester_resp["data"]["year"])
        existing = self.db.query(KRS).filter_by(
            id_mahasiswa=id_mahasiswa,
            semester=semester_nama,
            tahun_ajaran=tahun_ajaran
        ).first()
        if existing:
            return {"status": "skipped", "message": "KRS sudah ada", "id_mahasiswa": id_mahasiswa}
        krs = KRS(
            id_mahasiswa=id_mahasiswa,
            semester=semester_nama,
            tahun_ajaran=tahun_ajaran,
        )
        self.db.add(krs)
        self.db.flush()
        details = self.prs.get_prs_detail_by_prs_id(id_prs)
        if isinstance(details, dict) and "error" in details:
            return {"status": "error", "message": details["error"]}
        for detail in details:
            nilai = Nilai(
                id_krs       = krs.id_krs,
                id_mahasiswa = id_mahasiswa,
                id_matkul    = detail["id_mata_kuliah"],
                id_kelas     = detail["id_kelas"],
                nilai_uts    = None,
                nilai_uas    = None,
                nilai_tes1   = None,
                nilai_tes2   = None,
                status       = StatusNilai.BELUM_TERNILAI,
            )
            self.db.add(nilai)
        self.db.commit()
        return {
            "status": "ok",
            "id_mahasiswa": id_mahasiswa,
            "id_krs": krs.id_krs,
            "jumlah_matkul": len(details)
        }