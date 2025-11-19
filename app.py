import mysql.connector
from mysql.connector import Error
import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS
from collections import defaultdict

# --- Konfigurasi Aplikasi ---
app = Flask(__name__)
CORS(app)

# --- KONFIGURASI DATABASE MYSQL ---
DB_CONFIG = {
    'host': 'localhost',        
    'user': 'root',             
    'password': '', # GANTI DENGAN PASSWORD ANDA (jika ada)
    'database': 'smartlock_db'  
}
# ------------------------------------

CORRECT_PIN = "1234"

# --- Fungsi Inisialisasi Database ---
def init_db():
    """Membuat tabel 'door_status', 'logs', dan 'config' jika belum ada"""
    print("Mencoba koneksi ke MySQL dan inisialisasi tabel...")
    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(
            host=DB_CONFIG['host'],
            user=DB_CONFIG['user'],
            password=DB_CONFIG['password'],
            database=DB_CONFIG['database']
        )
        cursor = conn.cursor()
        
        # 1. Tabel Status Pintu
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS door_status (
            id INT PRIMARY KEY,
            locked BOOLEAN NOT NULL,
            last_access DATETIME NOT NULL
        )
        ''')
        
        # 2. Tabel Log
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            id INT PRIMARY KEY AUTO_INCREMENT,
            action VARCHAR(20) NOT NULL, 
            timestamp DATETIME NOT NULL,
            success BOOLEAN NOT NULL
        )
        ''')
        
        # 3. Tabel Konfigurasi (DIPERBARUI DENGAN JADWAL)
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS config (
            id INT PRIMARY KEY,
            auto_lock_delay INT NOT NULL DEFAULT 30,
            schedule_enabled BOOLEAN NOT NULL DEFAULT 0,
            schedule_lock_time VARCHAR(5) NOT NULL DEFAULT '22:00',
            schedule_unlock_time VARCHAR(5) NOT NULL DEFAULT '06:00',
            updated_at DATETIME NOT NULL
        )
        ''')

        # --- Masukkan Data Default ---
        
        # Status default
        cursor.execute("SELECT * FROM door_status WHERE id = 1")
        if cursor.fetchone() is None:
            cursor.execute("INSERT INTO door_status (id, locked, last_access) VALUES (%s, %s, %s)", 
                           (1, True, datetime.datetime.now()))
            print("Status pintu default (terkunci) dibuat.")

        # Konfigurasi default (DIPERBARUI)
        cursor.execute("SELECT * FROM config WHERE id = 1")
        if cursor.fetchone() is None:
            query = """
                INSERT INTO config 
                (id, auto_lock_delay, schedule_enabled, schedule_lock_time, schedule_unlock_time, updated_at) 
                VALUES (%s, %s, %s, %s, %s, %s)
            """
            values = (1, 30, False, '22:00', '06:00', datetime.datetime.now())
            cursor.execute(query, values)
            print("Pengaturan config default (auto-lock & schedule) dibuat.")

        # (PENTING) Modifikasi tabel config jika sudah ada tapi kolom baru belum ada
        try:
            cursor.execute("ALTER TABLE config ADD COLUMN schedule_enabled BOOLEAN NOT NULL DEFAULT 0")
            cursor.execute("ALTER TABLE config ADD COLUMN schedule_lock_time VARCHAR(5) NOT NULL DEFAULT '22:00'")
            cursor.execute("ALTER TABLE config ADD COLUMN schedule_unlock_time VARCHAR(5) NOT NULL DEFAULT '06:00'")
            conn.commit()
            print("Kolom jadwal ditambahkan ke tabel config yang ada.")
        except Error as e:
            if e.errno == 1060: # Error: Kolom sudah ada
                pass # Abaikan jika kolom sudah ada, itu normal
            else:
                raise # Tampilkan error lain

        conn.commit()
        print("Database dan tabel (door_status, logs, config) siap (MySQL).")
        
    except Error as e:
        print(f"Error saat inisialisasi database MySQL: {e}")
    finally:
        if cursor:
            cursor.close()
        if 'conn' in locals() and conn and conn.is_connected():
            conn.close()

# --- Fungsi Helper Database ---
def get_db_conn():
    """Membuka koneksi baru ke DB MySQL"""
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        return conn
    except Error as e:
        print(f"Error koneksi ke MySQL: {e}")
        return None

def log_activity(action, success):
    """Mencatat aktivitas ke tabel logs"""
    conn = None
    cursor = None
    try:
        conn = get_db_conn()
        if conn:
            cursor = conn.cursor()
            query = "INSERT INTO logs (action, timestamp, success) VALUES (%s, %s, %s)"
            values = (action, datetime.datetime.now(), success)
            cursor.execute(query, values)
            conn.commit()
    except Error as e:
        print(f"Error logging activity: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()

# --- (BARU) Helper Pengunci & Pembuka ---
def _perform_lock(conn, reason="lock"):
    """Fungsi internal untuk mengunci pintu & log"""
    cursor = conn.cursor()
    query = "UPDATE door_status SET locked = %s, last_access = %s WHERE id = %s"
    values = (True, datetime.datetime.now(), 1)
    cursor.execute(query, values)
    conn.commit()
    log_activity(reason, True) # Log dengan alasan (misal: 'lock (auto)')
    cursor.close()
    print(f"LOCK: Pintu telah terkunci (Alasan: {reason}).")

def _perform_unlock(conn, reason="unlock"):
    """Fungsi internal untuk membuka kunci pintu & log"""
    cursor = conn.cursor()
    query = "UPDATE door_status SET locked = %s, last_access = %s WHERE id = %s"
    values = (False, datetime.datetime.now(), 1)
    cursor.execute(query, values)
    conn.commit()
    log_activity(reason, True) # Log dengan alasan (misal: 'unlock (schedule)')
    cursor.close()
    print(f"UNLOCK: Pintu telah dibuka (Alasan: {reason}).")


# --- ENDPOINT API INTI ---

# Endpoint 1: Cek Status Pintu (LOGIKA PALING PENTING)
@app.route('/door/status', methods=['GET'])
def get_door_status():
    conn = None
    cursor = None
    try:
        conn = get_db_conn()
        if not conn:
            return jsonify({"error": "Koneksi database gagal"}), 500
            
        cursor = conn.cursor(dictionary=True) 
        
        # --- (CHECK 1: LOGIKA AUTO-LOCK TIMER) ---
        cursor.execute("SELECT locked, last_access FROM door_status WHERE id = 1")
        status = cursor.fetchone()
        cursor.execute("SELECT auto_lock_delay FROM config WHERE id = 1")
        config = cursor.fetchone()
        
        is_locked = bool(status['locked'])
        last_access_time = status['last_access']
        delay_seconds = config['auto_lock_delay']

        if not is_locked and delay_seconds > 0:
            time_since_unlocked = datetime.datetime.now() - last_access_time
            if time_since_unlocked.total_seconds() > delay_seconds:
                _perform_lock(conn, "lock (auto)")
                # Ambil ulang status baru
                cursor.execute("SELECT locked, last_access FROM door_status WHERE id = 1")
                status = cursor.fetchone()
                is_locked = bool(status['locked']) # Perbarui variabel status
        
        # --- (CHECK 2: LOGIKA SCHEDULED LOCK/UNLOCK) ---
        cursor.execute("SELECT schedule_enabled, schedule_lock_time, schedule_unlock_time FROM config WHERE id = 1")
        schedule_config = cursor.fetchone()
        
        if schedule_config['schedule_enabled']:
            now = datetime.datetime.now()
            current_time_str = now.strftime('%H:%M')
            
            # Cek apakah sekarang waktunya KUNCI
            if current_time_str == schedule_config['schedule_lock_time'] and not is_locked:
                _perform_lock(conn, "lock (schedule)")
                cursor.execute("SELECT locked, last_access FROM door_status WHERE id = 1")
                status = cursor.fetchone()
                is_locked = bool(status['locked'])
            
            # Cek apakah sekarang waktunya BUKA KUNCI
            elif current_time_str == schedule_config['schedule_unlock_time'] and is_locked:
                _perform_unlock(conn, "unlock (schedule)")
                cursor.execute("SELECT locked, last_access FROM door_status WHERE id = 1")
                status = cursor.fetchone()
                
        if status:
            status['locked'] = bool(status['locked'])
            return jsonify(status)
        return jsonify({"error": "Status tidak ditemukan"}), 404
    
    except Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()

# Endpoint 2: Mengunci Pintu
@app.route('/door/lock', methods=['POST'])
def lock_door():
    conn = None
    try:
        conn = get_db_conn()
        if not conn:
            return jsonify({"error": "Koneksi database gagal"}), 500
        _perform_lock(conn, "lock (manual)")
        return jsonify({"success": True, "message": "Door locked"})
    except Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

# Endpoint 3: Membuka Kunci Pintu
@app.route('/door/unlock', methods=['POST'])
def unlock_door():
    data = request.get_json()
    pin = data.get('pin')
    
    if pin == CORRECT_PIN:
        conn = None
        try:
            conn = get_db_conn()
            if not conn:
                return jsonify({"error": "Koneksi database gagal"}), 500
            _perform_unlock(conn, "unlock (manual)")
            return jsonify({"success": True, "message": "Door unlocked"})
        except Error as e:
            return jsonify({"error": str(e)}), 500
        finally:
            if conn and conn.is_connected():
                conn.close()
    else:
        log_activity("unlock (fail)", False) 
        return jsonify({"success": False, "message": "Invalid PIN"}), 401

# Endpoint 4: Melihat Log Aktivitas
@app.route('/logs', methods=['GET'])
def get_logs():
    conn = None
    cursor = None
    try:
        conn = get_db_conn()
        if not conn:
            return jsonify({"error": "Koneksi database gagal"}), 500
            
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT action, timestamp, success FROM logs ORDER BY timestamp DESC LIMIT 20")
        logs = cursor.fetchall()
        
        for log in logs:
            log['success'] = bool(log['success'])
            
        return jsonify(logs)
    except Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()

# Endpoint 5: Cek Kesehatan Sistem
@app.route('/health', methods=['GET'])
def health_check():
    # ... (tidak berubah) ...
    conn = None
    try:
        conn = get_db_conn()
        if conn and conn.is_connected():
            return jsonify({"status": "online", "database": "MySQL connected"})
        else:
            return jsonify({"status": "offline", "database": "MySQL connection failed"}), 503
    finally:
        if conn and conn.is_connected():
            conn.close()

# --- ENDPOINT PENGATURAN / KONFIGURASI ---

# Endpoint 6: Mengambil Pengaturan
@app.route('/config', methods=['GET'])
def get_config():
    conn = None
    cursor = None
    try:
        conn = get_db_conn()
        if not conn:
            return jsonify({"error": "Koneksi database gagal"}), 500
            
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT auto_lock_delay, schedule_enabled, schedule_lock_time, schedule_unlock_time FROM config WHERE id = 1")
        config = cursor.fetchone()
        config['schedule_enabled'] = bool(config['schedule_enabled']) # Konversi ke boolean
        return jsonify(config)
    except Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()

# Endpoint 7: Memperbarui Pengaturan
@app.route('/config', methods=['PUT'])
def update_config():
    data = request.get_json()
    conn = None
    cursor = None
    try:
        # Ambil dan validasi semua data
        new_delay = int(data.get('auto_lock_delay', 30))
        new_schedule_enabled = bool(data.get('schedule_enabled', False))
        new_schedule_lock = data.get('schedule_lock_time', '22:00')
        new_schedule_unlock = data.get('schedule_unlock_time', '06:00')
        
        conn = get_db_conn()
        if not conn:
            return jsonify({"error": "Koneksi database gagal"}), 500
            
        cursor = conn.cursor()
        query = """
            UPDATE config 
            SET auto_lock_delay = %s, 
                schedule_enabled = %s, 
                schedule_lock_time = %s, 
                schedule_unlock_time = %s, 
                updated_at = %s 
            WHERE id = %s
        """
        values = (new_delay, new_schedule_enabled, new_schedule_lock, new_schedule_unlock, datetime.datetime.now(), 1)
        cursor.execute(query, values)
        conn.commit()
        return jsonify({"success": True, "message": "Pengaturan disimpan."})
    except (Error, ValueError, TypeError) as e:
        if conn:
            conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()


# --- ANALYTICS ENDPOINTS ---

# Analisis 1: Aktivitas per Jam
@app.route('/analytics/activity_by_hour', methods=['GET'])
def get_activity_by_hour():
    # ... (tidak berubah) ...
    conn = None
    cursor = None
    try:
        conn = get_db_conn()
        if not conn:
            return jsonify({"error": "Koneksi database gagal"}), 500
        query = "SELECT HOUR(timestamp) as hour, action, COUNT(*) as count FROM logs WHERE success = 1 GROUP BY hour, action"
        cursor = conn.cursor(dictionary=True)
        cursor.execute(query)
        rows = cursor.fetchall()
        hourly_stats = defaultdict(lambda: {"locks": 0, "unlocks": 0})
        for row in rows:
            hour_key = int(row['hour'])
            if 'lock' in row['action']:
                hourly_stats[hour_key]['locks'] += row['count']
            elif 'unlock' in row['action']:
                hourly_stats[hour_key]['unlocks'] += row['count']
        result = [{"hour": hour, "locks": hourly_stats[hour]['locks'], "unlocks": hourly_stats[hour]['unlocks']} for hour in range(24)]
        return jsonify(result)
    except Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# Analisis 2: Aktivitas Harian
@app.route('/analytics/daily_activity', methods=['GET'])
def get_daily_activity():
    # ... (tidak berubah) ...
    conn = None
    cursor = None
    try:
        conn = get_db_conn()
        if not conn:
            return jsonify({"error": "Koneksi database gagal"}), 500
        query = "SELECT DATE(timestamp) as event_date, COUNT(*) as total_events FROM logs WHERE success = 1 GROUP BY event_date ORDER BY event_date DESC LIMIT 30"
        cursor = conn.cursor(dictionary=True)
        cursor.execute(query)
        rows = cursor.fetchall()
        result = []
        for row in rows:
            row['event_date'] = row['event_date'].isoformat()
            result.append(row)
        return jsonify(result)
    except Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# Analisis 3: Durasi Tidak Terkunci
@app.route('/analytics/unlocked_duration', methods=['GET'])
def get_unlocked_duration():
    # ... (tidak berubah) ...
    conn = None
    cursor = None
    try:
        conn = get_db_conn()
        if not conn:
            return jsonify({"error": "Koneksi database gagal"}), 500
        query = """
            SELECT 
                t1.timestamp AS unlocked_at,
                (SELECT MIN(t2.timestamp) 
                 FROM logs t2 
                 WHERE t2.timestamp > t1.timestamp AND t2.action LIKE 'lock%' AND t2.success = 1) AS locked_at
            FROM logs t1
            WHERE t1.action LIKE 'unlock%' AND t1.success = 1
            ORDER BY t1.timestamp DESC
            LIMIT 50 
        """
        cursor = conn.cursor(dictionary=True)
        cursor.execute(query)
        rows = cursor.fetchall()
        durations = []
        for row in rows:
            unlocked_at_obj = row['unlocked_at']
            locked_at_obj = row['locked_at']
            duration_minutes = None
            if locked_at_obj:
                duration = locked_at_obj - unlocked_at_obj
                duration_minutes = round(duration.total_seconds() / 60, 1)
            durations.append({
                "unlocked_at": unlocked_at_obj.isoformat(),
                "locked_at": locked_at_obj.isoformat() if locked_at_obj else None,
                "duration_minutes": duration_minutes
            })
        return jsonify(durations)
    except Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# Analisis 4: Deteksi Ancaman
@app.route('/analytics/threats', methods=['GET'])
def get_threat_logs():
    # ... (tidak berubah) ...
    conn = None
    cursor = None
    try:
        conn = get_db_conn()
        if not conn:
            return jsonify({"error": "Koneksi database gagal"}), 500
        query = "SELECT timestamp, action FROM logs WHERE success = 0 ORDER BY timestamp DESC LIMIT 50"
        cursor = conn.cursor(dictionary=True)
        cursor.execute(query)
        rows = cursor.fetchall()
        threats = []
        for row in rows:
            threats.append({
                "timestamp": row['timestamp'].isoformat(),
                "action": row['action']
            })
        return jsonify(threats)
    except Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# Analisis 5: Total Buka/Tutup
@app.route('/analytics/total_counts', methods=['GET'])
def get_total_counts():
    conn = None
    cursor = None
    try:
        conn = get_db_conn()
        if not conn:
            return jsonify({"error": "Koneksi database gagal"}), 500
        
        # PERBAIKAN: Menggunakan query SQL yang jauh lebih baik untuk
        # menghitung total 'lock' dan 'unlock' secara terpisah
        # LIKE 'unlock%' HANYA akan cocok dengan 'unlock (manual)', 'unlock (schedule)', dll.
        # LIKE 'lock%' HANYA akan cocok dengan 'lock (manual)', 'lock (auto)', dll.
        query = """
            SELECT 
                (SELECT COUNT(*) FROM logs WHERE success = 1 AND action LIKE 'unlock%') AS total_unlocks,
                (SELECT COUNT(*) FROM logs WHERE success = 1 AND action LIKE 'lock%') AS total_locks
        """
        cursor = conn.cursor(dictionary=True)
        cursor.execute(query)
        
        # Query ini akan mengembalikan SATU baris
        counts = cursor.fetchone()
        
        if not counts:
             # Jika tabel kosong, kembalikan 0
            return jsonify({"total_locks": 0, "total_unlocks": 0})
            
        return jsonify(counts) # Langsung kembalikan hasilnya
        
    except Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()

# --- Menjalankan Server ---
if __name__ == '__main__':
    init_db() # Siapkan database dan tabel saat server pertama kali dijalankan
    app.run(debug=True, port=5000)