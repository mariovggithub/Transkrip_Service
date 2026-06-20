"""
gateway/service.py — Gateway HTTP untuk mengakses Transkrip Service.

Service ini TIDAK menyimpan data sendiri. Tugasnya hanya:
    1. Menerima request HTTP (dari Postman/client/service lain)
    2. Memvalidasi body request
    3. Meneruskan (RPC) ke transkrip_service
    4. Mengembalikan response HTTP

PENTING: nama service ini HARUS BERBEDA dari "transkrip_service"
(yang dipakai oleh Transkrip/service.py), karena dua service tidak
boleh memakai nama yang sama dalam satu proses Nameko.
"""
import json

from nameko.exceptions import BadRequest
from nameko.rpc import RpcProxy
from werkzeug import Response

from gateway.entrypoints import http


class GatewayService:
    name = "gateway_service"

    # RPC client untuk komunikasi dengan service Transkrip
    transkrip_rpc = RpcProxy("transkrip_service")

    # ═══════════════════════════════════════════════════════════
    # PUSH PRS → KRS
    # ═══════════════════════════════════════════════════════════
    @http("POST", "/push_semester_ke_krs")
    def push_semester_ke_krs(self, request):
        """
        Endpoint HTTP untuk tarik semua peserta PRS tervalidasi
        dalam satu semester, lalu buat KRS + Nilai kosong untuk
        masing-masing mahasiswa.

        Body JSON: {"id_semester": 2}

        DIROMBAK dari /push_prs_ke_krs (per id_prs) menjadi
        /push_semester_ke_krs (per id_semester, banyak mahasiswa
        sekaligus) — menyesuaikan method yang tersedia di PRS
        service (push_peserta_to_transkrip).
        """
        try:
            data = request.get_json(force=True)
            id_semester = data["id_semester"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise BadRequest(f"Invalid request body: {e}")

        result = self.transkrip_rpc.push_semester_ke_krs(id_semester)

        if result.get("status") == "error":
            return Response(
                json.dumps(result),
                status=400,
                mimetype="application/json"
            )

        return Response(
            json.dumps(result),
            status=200,
            mimetype="application/json"
        )

    # ═══════════════════════════════════════════════════════════
    # INPUT NILAI (oleh dosen)
    # ═══════════════════════════════════════════════════════════
    @http("POST", "/input_nilai")
    def input_nilai(self, request):
        """
        Endpoint HTTP untuk dosen mengisi salah satu komponen nilai.

        Body JSON:
            {
                "id_nilai": 1,
                "komponen": "uts" | "uas" | "tes1" | "tes2",
                "nilai": 85.5
            }
        """
        try:
            data = request.get_json(force=True)
            id_nilai = data["id_nilai"]
            komponen = data["komponen"]
            nilai    = data["nilai"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise BadRequest(f"Invalid request body: {e}")

        result = self.transkrip_rpc.input_nilai(id_nilai, komponen, nilai)

        status_code = 200 if result.get("status") == "ok" else 400
        return Response(
            json.dumps(result),
            status=status_code,
            mimetype="application/json"
        )

    # ═══════════════════════════════════════════════════════════
    # READ — Nilai per kelas
    # ═══════════════════════════════════════════════════════════
    @http("GET", "/nilai/kelas/<int:id_kelas>")
    def get_nilai_by_kelas(self, request, id_kelas):
        """
        Ambil semua nilai mahasiswa dalam satu kelas.

        Contoh: GET /nilai/kelas/1
        """
        result = self.transkrip_rpc.get_nilai_by_kelas(id_kelas)
        return Response(
            json.dumps(result),
            status=200,
            mimetype="application/json"
        )

    # ═══════════════════════════════════════════════════════════
    # READ — KHS per mahasiswa per semester
    # ═══════════════════════════════════════════════════════════
    @http("GET", "/khs/<int:id_mahasiswa>/<string:tahun_ajaran>/<string:semester>")
    def get_khs_by_mahasiswa(self, request, id_mahasiswa, tahun_ajaran, semester):
        """
        Ambil KHS mahasiswa untuk semester tertentu.

        Contoh: GET /khs/1/2024-2025/Ganjil
        """
        result = self.transkrip_rpc.get_khs_by_mahasiswa(id_mahasiswa, semester, tahun_ajaran)
        return Response(
            json.dumps(result),
            status=200,
            mimetype="application/json"
        )

    # ═══════════════════════════════════════════════════════════
    # READ — KRS mahasiswa (debugging)
    # ═══════════════════════════════════════════════════════════
    @http("GET", "/krs/<int:id_mahasiswa>")
    def get_krs_by_mahasiswa(self, request, id_mahasiswa):
        """
        Ambil semua KRS milik seorang mahasiswa.

        Contoh: GET /krs/1
        """
        result = self.transkrip_rpc.get_krs_by_mahasiswa(id_mahasiswa)
        return Response(
            json.dumps(result),
            status=200,
            mimetype="application/json"
        )

    # ═══════════════════════════════════════════════════════════
    # READ — Transkrip lengkap mahasiswa
    # ═══════════════════════════════════════════════════════════
    @http("GET", "/transkrip/<int:id_mahasiswa>")
    def get_transkrip_mahasiswa(self, request, id_mahasiswa):
        """
        Ambil transkrip lengkap mahasiswa (semua matkul lintas semester + IPK).

        Contoh: GET /transkrip/1
        """
        result = self.transkrip_rpc.get_transkrip_mahasiswa(id_mahasiswa)

        status_code = 200 if result.get("status") != "error" else 404
        return Response(
            json.dumps(result),
            status=status_code,
            mimetype="application/json"
        )

    # ═══════════════════════════════════════════════════════════
    # READ — Riwayat IPS per semester
    # ═══════════════════════════════════════════════════════════
    @http("GET", "/ips/<int:id_mahasiswa>")
    def get_ips_per_semester(self, request, id_mahasiswa):
        """
        Ambil riwayat IPS mahasiswa per semester.

        Contoh: GET /ips/1
        """
        result = self.transkrip_rpc.get_ips_per_semester(id_mahasiswa)
        return Response(
            json.dumps(result),
            status=200,
            mimetype="application/json"
        )

    # ═══════════════════════════════════════════════════════════
    # READ — IPK terkini
    # ═══════════════════════════════════════════════════════════
    @http("GET", "/ipk/<int:id_mahasiswa>")
    def get_ipk_mahasiswa(self, request, id_mahasiswa):
        """
        Ambil IPK terkini mahasiswa.

        Contoh: GET /ipk/1
        """
        result = self.transkrip_rpc.get_ipk_mahasiswa(id_mahasiswa)
        return Response(
            json.dumps(result),
            status=200,
            mimetype="application/json"
        )
