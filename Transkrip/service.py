"""
service.py — Entry point utama Nameko microservice Transkrip.

Semua method yang ditandai @rpc bisa dipanggil oleh service lain
(atau client) melalui RabbitMQ secara asynchronous.

Alur utama:
    PRS approved → push_prs_ke_krs() → KRS + Nilai kosong terbuat
    Dosen input nilai → input_nilai() → cek lengkap → KHS + Transkrip diperbarui
    Client baca → get_khs_*() / get_transkrip_*() / get_ips_*() / get_ipk_*()
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
    """
    Nameko service class.
    `name` adalah identifier service di RabbitMQ.
    Service lain memanggil method di sini via RpcProxy("transkrip_service").
    """
    name = "transkrip_service"

    # DatabaseSession adalah Nameko dependency injection untuk SQLAlchemy.
    # Otomatis handle session lifecycle (open/commit/rollback/close)
    # per request, sehingga tidak perlu manual session management.
    db = DatabaseSession(Base)

    # RpcProxy memungkinkan memanggil method di service lain
    # melalui RabbitMQ, seolah-olah memanggil fungsi Python biasa.
    master = RpcProxy("master_service")       # Grup A — data mahasiswa, matkul
    prs    = RpcProxy("prs_service")          # Grup E — data PRS tervalidasi

    # ═══════════════════════════════════════════════════════════
    # BAGIAN 1: INISIALISASI KRS
    # Dipanggil oleh service PRS setelah dosen wali approve PRS mahasiswa
    # ═══════════════════════════════════════════════════════════

    @rpc
    def push_semester_ke_krs(self, id_semester: int):
        """
        Tarik semua peserta tervalidasi dari PRS untuk satu semester,
        lalu buat KRS + Nilai kosong untuk masing-masing mahasiswa.

        DIROMBAK: sebelumnya nerima id_prs satu-satu, sekarang nerima
        id_semester dan proses banyak mahasiswa sekaligus — menyesuaikan
        method PRS yang tersedia (push_peserta_to_transkrip), karena
        PRS tidak punya method untuk lookup id_mahasiswa dari id_prs saja.
        """
        if id_semester is None:
            return {"status": "error", "message": "id_semester wajib diisi"}

        # Ambil data semester dari master_service (untuk nama semester + tahun ajaran)
        semester_resp = self.master.get_semester_by_id(id_semester)
        if semester_resp.get("status") != "success":
            return {"status": "error", "message": f"Semester {id_semester} tidak ditemukan di master_service"}
        semester_data = semester_resp["data"]
        semester_nama = semester_data["name"]
        tahun_ajaran  = str(semester_data["year"])

        # Ambil semua peserta tervalidasi dari PRS untuk semester ini
        prs_resp = self.prs.push_peserta_to_transkrip(id_semester)
        if "error" in prs_resp:
            return {"status": "error", "message": prs_resp["error"]}
        peserta_list = prs_resp.get("peserta", [])

        # Kelompokkan dulu per mahasiswa — satu mahasiswa bisa ambil banyak matkul,
        # jadi satu KRS per mahasiswa, tapi banyak baris Nilai per KRS.
        per_mahasiswa = {}
        for peserta in peserta_list:
            id_mhs = peserta["id_mahasiswa"]
            per_mahasiswa.setdefault(id_mhs, []).append(peserta)

        hasil = {"berhasil": [], "dilewati": []}

        for id_mahasiswa, daftar_matkul in per_mahasiswa.items():
            # Cek duplikasi: satu mahasiswa hanya boleh punya satu KRS per semester
            existing = self.db.query(KRS).filter_by(
                id_mahasiswa=id_mahasiswa, semester=semester_nama, tahun_ajaran=tahun_ajaran
            ).first()
            if existing:
                hasil["dilewati"].append({"id_mahasiswa": id_mahasiswa, "alasan": "KRS sudah ada"})
                continue

            krs = KRS(
                id_mahasiswa = id_mahasiswa,
                semester     = semester_nama,
                tahun_ajaran = tahun_ajaran,
            )
            self.db.add(krs)
            self.db.flush()  # supaya krs.id_krs ter-generate sebelum dipakai di bawah

            for matkul in daftar_matkul:
                nilai = Nilai(
                    id_krs    = krs.id_krs,
                    id_matkul = matkul["id_mata_kuliah"],   # field PRS: id_mata_kuliah -> field kita: id_matkul
                    id_kelas  = matkul["id_kelas"],
                    nilai_uts  = None,
                    nilai_uas  = None,
                    nilai_tes1 = None,
                    nilai_tes2 = None,
                    status     = StatusNilai.BELUM_TERNILAI,
                )
                self.db.add(nilai)

            hasil["berhasil"].append({"id_mahasiswa": id_mahasiswa, "id_krs": krs.id_krs, "jumlah_matkul": len(daftar_matkul)})

        self.db.commit()
        return {"status": "ok", **hasil}

    # ═══════════════════════════════════════════════════════════
    # BAGIAN 2: INPUT NILAI OLEH DOSEN
    # ═══════════════════════════════════════════════════════════

    @rpc
    def input_nilai(self, id_nilai: int, komponen: str, nilai: float):
        """
        Dosen mengisi salah satu komponen nilai untuk satu mata kuliah.
        Komponen bisa diisi bertahap (tidak harus sekaligus).

        Saat semua 4 komponen terisi:
            1. Nilai akhir dan huruf dihitung otomatis
            2. Status Nilai → SUDAH_TERNILAI
            3. KHS dan KHS_DETAIL dibuat/diperbarui
            4. Detail Transkrip diisi
            5. IPK mahasiswa dihitung ulang

        Dipanggil oleh: client (atas nama dosen)

        Args:
            id_nilai  : ID record Nilai yang akan diisi
            komponen  : "uts" | "uas" | "tes1" | "tes2"
            nilai     : Nilai angka (0.0 – 100.0)

        Returns:
            {"status": "ok", "nilai_huruf": str | None}
            {"status": "error", "message": str}
        """
        # Validasi komponen sebelum setattr agar tidak ada injeksi
        if not validasi_komponen(komponen):
            return {
                "status": "error",
                "message": f"Komponen '{komponen}' tidak valid. Gunakan: uts, uas, tes1, tes2"
            }

        # Validasi tipe & rentang nilai
        try:
            nilai = float(nilai)
        except (TypeError, ValueError):
            return {"status": "error", "message": "Nilai harus berupa angka"}

        if not (0.0 <= nilai <= 100.0):
            return {"status": "error", "message": "Nilai harus antara 0 dan 100"}

        record = self.db.query(Nilai).filter_by(id_nilai=id_nilai).first()
        if not record:
            return {"status": "error", "message": f"Nilai dengan id {id_nilai} tidak ditemukan"}

        # Set komponen yang dikirim (uts/uas/tes1/tes2)
        setattr(record, f"nilai_{komponen}", nilai)

        # Cek apakah semua komponen sekarang sudah terisi
        if semua_nilai_lengkap(record.nilai_uts, record.nilai_uas, record.nilai_tes1, record.nilai_tes2):
            # Hitung nilai akhir
            akhir        = hitung_nilai_akhir(record.nilai_uts, record.nilai_uas, record.nilai_tes1, record.nilai_tes2)
            huruf, bobot = nilai_ke_huruf(akhir)

            record.nilai_akhir = akhir
            record.nilai_huruf = huruf
            record.status      = StatusNilai.SUDAH_TERNILAI

            # Ambil KRS untuk mendapat konteks semester & mahasiswa
            krs = self.db.query(KRS).filter_by(id_krs=record.id_krs).first()

            # Ambil data matkul dari Master untuk mendapat SKS dan nama
            matkul_resp = self.master.get_course_by_id(record.id_matkul)
            matkul_data = matkul_resp.get("data", {}) if matkul_resp.get("status") == "success" else {}
            sks         = matkul_data.get("sks", 0)
            nama_matkul = matkul_data.get("name", f"Matkul #{record.id_matkul}")

            # Buat/update KHS untuk KRS ini
            self._update_khs(krs, record, sks)

            # Isi Detail Transkrip
            self._update_detail_transkrip(krs, record, sks, nama_matkul)

            # Hitung ulang IPK mahasiswa
            self._hitung_ulang_ipk(krs.id_mahasiswa)

        self.db.commit()
        return {"status": "ok", "nilai_huruf": record.nilai_huruf}

    def _update_khs(self, krs: KRS, nilai: Nilai, sks: int):
        """
        Internal: Buat atau update KHS dan KHS_DETAIL untuk satu KRS.
        Setelah update, hitung ulang IPS semester ini.

        Dipanggil dari input_nilai() saat nilai lengkap.
        """
        # Buat KHS jika belum ada untuk KRS ini
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

        # Cek apakah KHS_DETAIL untuk Nilai ini sudah ada (update, bukan duplikat)
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
            # Update jika dosen mengkoreksi nilai
            existing_detail.nilai_huruf = nilai.nilai_huruf
            existing_detail.nilai_akhir = nilai.nilai_akhir
            existing_detail.sks         = sks

        self.db.flush()

        # Hitung ulang IPS semester ini berdasarkan semua KHS_DETAIL yang sudah ada
        semua_detail = self.db.query(KHSDetail).filter_by(id_khs=khs.id_khs).all()
        detail_list  = []
        for d in semua_detail:
            _, bobot = nilai_ke_huruf(d.nilai_akhir)
            detail_list.append({"sks": d.sks, "bobot": bobot})

        khs.ips = hitung_ips(detail_list)

    def _update_detail_transkrip(self, krs: KRS, nilai: Nilai, sks: int, nama_matkul: str):
        """
        Isi tabel detail_transkrip.

        Setiap matkul yang sudah ternilai masuk ke detail_transkrip sebagai
        rekap keseluruhan riwayat akademik mahasiswa.
        """
        # Pastikan Transkrip mahasiswa sudah ada
        transkrip = self.db.query(Transkrip).filter_by(
            id_mahasiswa=krs.id_mahasiswa
        ).first()
        if not transkrip:
            transkrip = Transkrip(id_mahasiswa=krs.id_mahasiswa)
            self.db.add(transkrip)
            self.db.flush()

        # Cek apakah detail untuk nilai ini sudah ada (update, bukan duplikat)
        existing = self.db.query(DetailTranskrip).filter_by(id_nilai=nilai.id_nilai).first()
        if not existing:
            detail = DetailTranskrip(
                id_transkrip = transkrip.id_transkrip,
                id_nilai     = nilai.id_nilai,
                id_matkul    = nilai.id_matkul,
                nama_matkul  = nama_matkul,     # Di-cache agar tidak RPC lagi saat read
                semester     = krs.semester,
                tahun_ajaran = krs.tahun_ajaran,
                sks          = sks,
                nilai_huruf  = nilai.nilai_huruf,
                nilai_akhir  = nilai.nilai_akhir,
            )
            self.db.add(detail)
        else:
            # Update jika dosen mengkoreksi nilai
            existing.nilai_huruf = nilai.nilai_huruf
            existing.nilai_akhir = nilai.nilai_akhir
            existing.sks         = sks

        self.db.flush()

    def _hitung_ulang_ipk(self, id_mahasiswa: int):
        """
        Hitung ulang IPK dan total SKS mahasiswa berdasarkan
        semua DetailTranskrip yang sudah ada.

        Dipanggil setiap kali ada nilai baru yang lengkap.
        """
        transkrip = self.db.query(Transkrip).filter_by(
            id_mahasiswa=id_mahasiswa
        ).first()
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

    # ═══════════════════════════════════════════════════════════
    # BAGIAN 3: READ — Dibaca oleh Client / Service Lain
    # ═══════════════════════════════════════════════════════════

    @rpc
    def get_nilai_by_kelas(self, id_kelas: int):
        """
        Ambil semua nilai mahasiswa dalam satu kelas.
        Dipakai oleh dosen untuk melihat nilai seluruh mahasiswanya.

        Dipanggil oleh: client (atas nama dosen)
        """
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
                # .value agar hasil berupa string biasa, bukan objek Enum
                # (objek Enum tidak bisa di-serialize langsung oleh RPC/JSON)
                "status":      n.status.value if n.status else None,
            }
            for n in nilai_list
        ]

    @rpc
    def get_khs_by_mahasiswa(self, id_mahasiswa: int, semester: str, tahun_ajaran: str):
        """
        Ambil KHS mahasiswa untuk semester tertentu.
        Berisi daftar matkul beserta nilai dan IPS semester.

        Dipanggil oleh: client, perwalian_service
        """
        # Cari semua KRS mahasiswa di semester ini
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

            ips_semester = khs.ips  # Semua KRS di semester sama punya IPS sama

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
        Ambil transkrip lengkap mahasiswa:
        semua matkul lintas semester + IPK + total SKS.

        Dipanggil oleh: client, perwalian_service, prs_service
        """
        transkrip = self.db.query(Transkrip).filter_by(
            id_mahasiswa=id_mahasiswa
        ).first()

        if not transkrip:
            return {"status": "error", "message": "Transkrip belum tersedia"}

        # Ambil data mahasiswa dari Master service
        try:
            mhs_resp = self.master.get_student_by_id(id_mahasiswa)
            mahasiswa = mhs_resp.get("data", {"id_mahasiswa": id_mahasiswa}) if mhs_resp.get("status") == "success" else {"id_mahasiswa": id_mahasiswa}
        except Exception:
            mahasiswa = {"id_mahasiswa": id_mahasiswa}

        # Ambil semua detail matkul
        details = self.db.query(DetailTranskrip).filter_by(
            id_transkrip=transkrip.id_transkrip
        ).order_by(DetailTranskrip.tahun_ajaran, DetailTranskrip.semester).all()

        # Kelompokkan per semester untuk tampilan seperti SIM Petra
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
            "status":     "ok",
            "mahasiswa":  mahasiswa,
            "total_sks":  transkrip.total_sks,
            "ipk":        transkrip.ipk,
            "riwayat":    list(per_semester.values()),
        }

    @rpc
    def get_ips_per_semester(self, id_mahasiswa: int):
        """
        Ambil riwayat IPS mahasiswa per semester.

        Dipanggil oleh: client, prs_service (untuk cek syarat pengambilan matkul)
        """
        # Cari semua KRS mahasiswa ini
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

        # Urutkan berdasarkan tahun dan semester
        riwayat.sort(key=lambda x: (x["tahun_ajaran"], x["semester"]))
        return riwayat

    @rpc
    def get_ipk_mahasiswa(self, id_mahasiswa: int):
        """
        Ambil IPK terkini mahasiswa.

        Dipanggil oleh: client, perwalian_service, prs_service
        """
        transkrip = self.db.query(Transkrip).filter_by(
            id_mahasiswa=id_mahasiswa
        ).first()
        return {
            "id_mahasiswa": id_mahasiswa,
            "ipk":          transkrip.ipk if transkrip else 0.0,
            "total_sks":    transkrip.total_sks if transkrip else 0,
        }

    @rpc
    def get_krs_by_mahasiswa(self, id_mahasiswa: int):
        """
        Ambil semua KRS milik seorang mahasiswa (lintas semester).
        Berguna untuk debugging / verifikasi data dasar.
        """
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
