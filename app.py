import os, cv2, uuid, traceback, threading, math, json, hashlib
import numpy as np
from flask import Flask, request, jsonify, send_from_directory, render_template
from flask_cors import CORS
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = os.environ.get('SECRET_KEY', 'kickmetrics-2026')
CORS(app)

UPLOAD_FOLDER = '/tmp/km_uploads'
OUTPUT_FOLDER = '/tmp/km_outputs'
app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024 * 1024
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

jobs = {}  # legacy in-memory fallback (not reliable across restarts)

JOB_FOLDER = '/tmp/km_jobs'
os.makedirs(JOB_FOLDER, exist_ok=True)

# ════════════════════════════════════════
#  DATABASE — PostgreSQL via psycopg2
#  Falls back to in-memory dict if no DB
# ════════════════════════════════════════
try:
    import psycopg2
    import psycopg2.extras
    DB_URL = os.environ.get('DATABASE_URL', '')
    USE_DB = bool(DB_URL)
except ImportError:
    USE_DB = False

def get_db():
    if not USE_DB:
        return None
    conn = psycopg2.connect(DB_URL, sslmode='require')
    return conn

def init_db():
    """Create tables if they don't exist."""
    if not USE_DB:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS coaches (
                email TEXT PRIMARY KEY,
                data JSONB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS teams (
                id TEXT PRIMARY KEY,
                data JSONB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS players (
                email TEXT PRIMARY KEY,
                data JSONB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                data JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
        ''')
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print('DB init error:', e)

# ── In-memory fallback (resets on restart — use DB for production) ──
_mem = {'coaches': {}, 'teams': {}, 'players': {}}

def load_data():
    if not USE_DB:
        return _mem
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        data = {'coaches': {}, 'teams': {}, 'players': {}}
        cur.execute('SELECT email, data FROM coaches')
        for row in cur.fetchall():
            data['coaches'][row['email']] = row['data']
        cur.execute('SELECT id, data FROM teams')
        for row in cur.fetchall():
            data['teams'][row['id']] = row['data']
        cur.execute('SELECT email, data FROM players')
        for row in cur.fetchall():
            data['players'][row['email']] = row['data']
        cur.close(); conn.close()
        return data
    except Exception as e:
        print('load_data error:', e)
        return {'coaches': {}, 'teams': {}, 'players': {}}

def save_coach(email, coach_data):
    if not USE_DB:
        _mem['coaches'][email] = coach_data
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO coaches (email, data) VALUES (%s, %s) ON CONFLICT (email) DO UPDATE SET data = %s',
            (email, json.dumps(coach_data), json.dumps(coach_data))
        )
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print('save_coach error:', e)

def save_team(team_id, team_data):
    if not USE_DB:
        _mem['teams'][team_id] = team_data
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO teams (id, data) VALUES (%s, %s) ON CONFLICT (id) DO UPDATE SET data = %s',
            (team_id, json.dumps(team_data), json.dumps(team_data))
        )
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print('save_team error:', e)

def save_player(email, player_data):
    if not USE_DB:
        _mem['players'][email] = player_data
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO players (email, data) VALUES (%s, %s) ON CONFLICT (email) DO UPDATE SET data = %s',
            (email, json.dumps(player_data), json.dumps(player_data))
        )
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print('save_player error:', e)

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

# ════════════════════════════════════════
#  JOB PERSISTENCE
#  DB → PostgreSQL; fallback → /tmp files
# ════════════════════════════════════════
def get_job(jid):
    """Read job status. DB → file fallback → in-memory fallback."""
    if USE_DB:
        try:
            conn = get_db()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute('SELECT data FROM jobs WHERE id = %s', (jid,))
            row = cur.fetchone()
            cur.close(); conn.close()
            return dict(row['data']) if row else None
        except Exception as e:
            print('get_job DB error:', e)
    # file-based fallback
    fp = os.path.join(JOB_FOLDER, jid + '.json')
    if os.path.exists(fp):
        try:
            with open(fp) as f:
                return json.load(f)
        except Exception:
            pass
    # last resort: in-memory (same process only)
    return jobs.get(jid)

def set_job(jid, data):
    """Write job status. DB → file fallback → in-memory fallback."""
    if USE_DB:
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                'INSERT INTO jobs (id, data) VALUES (%s, %s) ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data',
                (jid, json.dumps(data))
            )
            conn.commit(); cur.close(); conn.close()
            return
        except Exception as e:
            print('set_job DB error:', e)
    # file-based fallback (survives gunicorn worker restarts within same container)
    fp = os.path.join(JOB_FOLDER, jid + '.json')
    try:
        with open(fp, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        print('set_job file error:', e)
    # also keep in-memory as a fast path
    jobs[jid] = data

def update_job(jid, patch):
    """Merge patch into existing job data and persist."""
    current = get_job(jid) or {}
    current.update(patch)
    set_job(jid, current)

# ── ROUTES ──
@app.route('/favicon.ico')
def favicon(): return '', 204

@app.route('/')
def index(): return render_template('index.html')

@app.route('/coach')
def coach_app(): return render_template('coach.html')

@app.route('/player')
def player_app(): return render_template('player.html')

@app.route('/analysis')
def analysis_page(): return render_template('analysis.html')

@app.route('/join/<team_code>')
def join_page(team_code): return render_template('index.html')

@app.route('/api/debug')
def debug_route():
    data = load_data()
    return jsonify({
        'db_connected': USE_DB,
        'coaches_count': len(data['coaches']),
        'teams_count': len(data['teams']),
        'players_count': len(data['players']),
        'team_codes': [t.get('code','?') for t in data['teams'].values()]
    })

# ── COACH SIGNUP ──
@app.route('/api/coach/signup', methods=['POST'])
def coach_signup():
    try:
        d = request.get_json()
        if not d:
            return jsonify({'error': 'No data received'}), 400
        data = load_data()
        email = (d.get('email') or '').lower().strip()
        password = d.get('password') or ''
        name = d.get('name') or ''
        team_name = d.get('team_name') or 'My Team'
        if not email or not password or not name:
            return jsonify({'error': 'Name, email and password are required'}), 400
        if email in data['coaches']:
            return jsonify({'error': 'Email already registered — please sign in'}), 400
        coach_id = str(uuid.uuid4())
        team_code = str(uuid.uuid4())[:8].upper()
        team_id = str(uuid.uuid4())
        coach_data = {'id': coach_id, 'name': name, 'email': email,
                      'password': hash_pw(password), 'team_id': team_id}
        team_data = {'id': team_id, 'name': team_name, 'code': team_code,
                     'coach_id': coach_id, 'logo_url': None,
                     'primary_colour': '#22a05a', 'players': [],
                     'team_goals': [], 'individual_goals': {}}
        save_coach(email, coach_data)
        save_team(team_id, team_data)
        return jsonify({'ok': True, 'coach_id': coach_id, 'team_id': team_id,
                        'team_code': team_code, 'name': name, 'team_name': team_name})
    except Exception as e:
        return jsonify({'error': str(e), 'detail': traceback.format_exc()}), 500

# ── COACH LOGIN ──
@app.route('/api/coach/login', methods=['POST'])
def coach_login():
    try:
        d = request.get_json()
        if not d: return jsonify({'error': 'No data received'}), 400
        data = load_data()
        email = (d.get('email') or '').lower().strip()
        coach = data['coaches'].get(email)
        if not coach or coach['password'] != hash_pw(d.get('password', '')):
            return jsonify({'error': 'Invalid email or password'}), 401
        team = data['teams'].get(coach['team_id'], {})
        return jsonify({'ok': True, 'coach_id': coach['id'],
                        'team_id': coach['team_id'], 'name': coach['name'],
                        'team_name': team.get('name', ''),
                        'team_code': team.get('code', '')})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── PLAYER SIGNUP ──
@app.route('/api/player/signup', methods=['POST'])
def player_signup():
    try:
        d = request.get_json()
        if not d:
            return jsonify({'error': 'No data received — please try again'}), 400
        data = load_data()
        email = (d.get('email') or '').lower().strip()
        password = d.get('password') or ''
        name = (d.get('name') or '').strip()
        team_code = (d.get('team_code') or '').upper().strip()

        # Detailed validation
        if not name:
            return jsonify({'error': 'Please enter your name'}), 400
        if not email:
            return jsonify({'error': 'Please enter your email address'}), 400
        if not password:
            return jsonify({'error': 'Please enter a password'}), 400
        if not team_code:
            return jsonify({'error': 'Please enter your team code'}), 400
        if len(team_code) < 4:
            return jsonify({'error': 'Team code seems too short — check with your coach'}), 400

        if email in data['players']:
            return jsonify({'error': 'Email already registered — please sign in instead'}), 400

        # Find team
        team = next((t for t in data['teams'].values()
                     if t.get('code', '').upper() == team_code), None)
        if not team:
            # Give helpful debug info
            existing_codes = [t.get('code', '') for t in data['teams'].values()]
            return jsonify({
                'error': 'Team code "' + team_code + '" not found. Double-check with your coach.',
                'hint': str(len(existing_codes)) + ' team(s) exist in the system'
            }), 400

        player_id = str(uuid.uuid4())
        player_data = {
            'id': player_id, 'name': name, 'email': email,
            'password': hash_pw(password), 'team_id': team['id'],
            'position': d.get('position', ''),
            'height': d.get('height', ''),
            'foot': d.get('foot', 'right'),
            'shirt_number': str(d.get('shirt_number', '')),
            'dob': d.get('dob', ''),
            'theme_colour': d.get('theme_colour', '#22a05a'),
            'matches': [],
            'season_stats': {
                'matches': 0, 'goals': 0, 'assists': 0, 'passes': 0,
                'shots': 0, 'tackles': 0, 'dribbles': 0,
                'distance': 0.0, 'time_on_ball': 0.0, 'performanceScore': 0
            }
        }

        if player_id not in team.get('players', []):
            team['players'].append(player_id)

        save_player(email, player_data)
        save_team(team['id'], team)

        goals = team.get('individual_goals', {}).get(player_id, [])
        return jsonify({
            'ok': True, 'player_id': player_id,
            'team_id': team['id'], 'team_name': team['name'],
            'name': name, 'position': player_data['position'],
            'foot': player_data['foot'],
            'shirt_number': player_data['shirt_number'],
            'theme_colour': player_data['theme_colour'],
            'season_stats': player_data['season_stats'],
            'goals': goals,
            'primary_colour': team.get('primary_colour', '#22a05a')
        })
    except Exception as e:
        return jsonify({'error': str(e), 'detail': traceback.format_exc()}), 500

# ── PLAYER LOGIN ──
@app.route('/api/player/login', methods=['POST'])
def player_login():
    try:
        d = request.get_json()
        if not d: return jsonify({'error': 'No data received'}), 400
        data = load_data()
        email = (d.get('email') or '').lower().strip()
        player = data['players'].get(email)
        if not player or player['password'] != hash_pw(d.get('password', '')):
            return jsonify({'error': 'Invalid email or password'}), 401
        team = data['teams'].get(player['team_id'], {})
        goals = team.get('individual_goals', {}).get(player['id'], [])
        return jsonify({
            'ok': True, 'player_id': player['id'],
            'team_id': player['team_id'], 'name': player['name'],
            'team_name': team.get('name', ''),
            'position': player.get('position', ''),
            'foot': player.get('foot', 'right'),
            'shirt_number': player.get('shirt_number', ''),
            'theme_colour': player.get('theme_colour', '#22a05a'),
            'season_stats': player.get('season_stats', {}),
            'goals': goals,
            'primary_colour': team.get('primary_colour', '#22a05a'),
            'matches': player.get('matches', [])
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── COACH: GET TEAM ──
@app.route('/api/coach/team/<team_id>', methods=['GET'])
def get_team(team_id):
    try:
        data = load_data()
        team = data['teams'].get(team_id)
        if not team: return jsonify({'error': 'Team not found'}), 404
        players = []
        for pid in team.get('players', []):
            p = next((pl for pl in data['players'].values() if pl['id'] == pid), None)
            if p:
                players.append({
                    'id': p['id'], 'name': p['name'],
                    'position': p.get('position', ''),
                    'foot': p.get('foot', ''),
                    'shirt_number': p.get('shirt_number', ''),
                    'height': p.get('height', ''),
                    'season_stats': p.get('season_stats', {}),
                    'matches': len(p.get('matches', []))
                })
        return jsonify({'team': team, 'players': players})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── COACH: UPDATE TEAM ──
@app.route('/api/coach/team/<team_id>', methods=['PUT'])
def update_team(team_id):
    try:
        d = request.get_json()
        data = load_data()
        team = data['teams'].get(team_id)
        if not team: return jsonify({'error': 'Team not found'}), 404
        for key in ['name', 'primary_colour', 'team_goals']:
            if key in d: team[key] = d[key]
        save_team(team_id, team)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── COACH: SET PLAYER GOALS ──
@app.route('/api/coach/goals/<team_id>/<player_id>', methods=['POST'])
def set_player_goals(team_id, player_id):
    try:
        d = request.get_json()
        data = load_data()
        team = data['teams'].get(team_id)
        if not team: return jsonify({'error': 'Team not found'}), 404
        if 'individual_goals' not in team: team['individual_goals'] = {}
        team['individual_goals'][player_id] = d.get('goals', [])
        save_team(team_id, team)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── COACH: UPLOAD LOGO ──
@app.route('/api/coach/logo/<team_id>', methods=['POST'])
def upload_logo(team_id):
    try:
        data = load_data()
        team = data['teams'].get(team_id)
        if not team: return jsonify({'error': 'Team not found'}), 404
        if 'logo' not in request.files: return jsonify({'error': 'No file'}), 400
        file = request.files['logo']
        ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'png'
        filename = team_id + '_logo.' + ext
        filepath = os.path.join(OUTPUT_FOLDER, filename)
        file.save(filepath)
        img = cv2.imread(filepath)
        if img is not None:
            team['primary_colour'] = extract_primary_colour(img)
        team['logo_url'] = '/output/' + filename
        save_team(team_id, team)
        return jsonify({'ok': True, 'logo_url': team['logo_url'],
                        'primary_colour': team['primary_colour']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── PLAYER: UPDATE THEME ──
@app.route('/api/player/theme/<player_id>', methods=['POST'])
def update_theme(player_id):
    try:
        d = request.get_json()
        data = load_data()
        player = next((p for p in data['players'].values() if p['id'] == player_id), None)
        if not player: return jsonify({'error': 'Player not found'}), 404
        player['theme_colour'] = d.get('colour', '#22a05a')
        save_player(player['email'], player)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── SAVE MATCH ──
@app.route('/api/player/match', methods=['POST'])
def save_match():
    try:
        d = request.get_json()
        player_id = d.get('player_id')
        data = load_data()
        player = next((p for p in data['players'].values() if p['id'] == player_id), None)
        if not player: return jsonify({'error': 'Player not found'}), 404
        match = {'id': str(uuid.uuid4()), 'date': d.get('date', ''),
                 'opposition': d.get('opposition', ''), 'result': d.get('result', ''),
                 'score': d.get('score', ''), 'stats': d.get('stats', {})}
        if 'matches' not in player: player['matches'] = []
        player['matches'].insert(0, match)
        s = d.get('stats', {})
        ss = player.get('season_stats', {})
        ss['matches'] = ss.get('matches', 0) + 1
        for k in ['goals', 'assists', 'passes', 'shots', 'tackles', 'dribbles']:
            ss[k] = ss.get(k, 0) + s.get(k, 0)
        ss['distance'] = round(ss.get('distance', 0) + s.get('metersRan', 0) / 1000, 2)
        ss['time_on_ball'] = round(ss.get('time_on_ball', 0) + s.get('timeOnBall', 0), 1)
        perf = s.get('performanceScore', 0)
        if perf > 0:
            prev = ss.get('performanceScore', 0)
            ss['performanceScore'] = round((prev + perf) / 2) if prev > 0 else perf
        player['season_stats'] = ss
        save_player(player['email'], player)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── VIDEO UPLOAD ──
@app.route('/api/upload', methods=['POST'])
def upload_video():
    try:
        if 'video' not in request.files: return jsonify({'error': 'No video'}), 400
        file = request.files['video']
        filename = str(uuid.uuid4()) + '_' + secure_filename(file.filename)
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        cap = cv2.VideoCapture(filepath)
        ret, frame = cap.read()
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        if not ret: return jsonify({'error': 'Cannot read video'}), 400
        h, w = frame.shape[:2]
        if w > 1280: frame = cv2.resize(frame, (1280, int(h * 1280 / w)))
        fid = str(uuid.uuid4())
        cv2.imwrite(os.path.join(OUTPUT_FOLDER, fid + '_frame.jpg'), frame)
        return jsonify({'video_id': filename, 'frame_id': fid,
                        'frame_url': '/output/' + fid + '_frame.jpg',
                        'fps': fps, 'total_frames': total,
                        'duration': round(total / fps, 1),
                        'width': frame.shape[1], 'height': frame.shape[0]})
    except Exception as e:
        return jsonify({'error': str(e), 'detail': traceback.format_exc()}), 500

@app.route('/output/<filename>')
def serve_output(filename): return send_from_directory(OUTPUT_FOLDER, filename)

# ── ANALYSE ──
@app.route('/api/analyse', methods=['POST'])
def analyse_video():
    try:
        d = request.get_json()
        vid = d.get('video_id'); bbox = d.get('bbox')
        if not vid or not bbox: return jsonify({'error': 'Missing fields'}), 400
        fp = os.path.join(UPLOAD_FOLDER, vid)
        if not os.path.exists(fp): return jsonify({'error': 'Video not found — re-upload'}), 404
        jid = str(uuid.uuid4())
        set_job(jid, {'status': 'running', 'progress': 0, 'step': 'Starting'})
        threading.Thread(target=run_job, args=(jid, fp, bbox, d.get('player_info', {}), d.get('prev_goals', '')), daemon=True).start()
        return jsonify({'job_id': jid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/job/<jid>')
def job_status(jid):
    j = get_job(jid)
    return jsonify(j) if j else (jsonify({'error': 'Not found'}), 404)


# ════════════════════════════════════════
#  COLOUR TOOLS
# ════════════════════════════════════════
def extract_primary_colour(img):
    try:
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        small = cv2.resize(img_rgb, (50, 50))
        pixels = small.reshape(-1, 3).astype(np.float32)
        mask = ~((pixels.max(axis=1) > 240) | (pixels.min(axis=1) < 15))
        filtered = pixels[mask]
        if len(filtered) < 10: return '#22a05a'
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 10, 1.0)
        _, _, centers = cv2.kmeans(filtered, 3, None, criteria, 3, cv2.KMEANS_RANDOM_CENTERS)
        best = sorted(centers, key=lambda c: np.std(c))[-1]
        r, g, b = [int(v) for v in best]
        return '#{:02x}{:02x}{:02x}'.format(r, g, b)
    except:
        return '#22a05a'

def build_sig(frame, x, y, bw, bh):
    py, px = int(bh * 0.2), int(bw * 0.1)
    rx = min(x + px, frame.shape[1] - 1); ry = min(y + py, frame.shape[0] - 1)
    rw = max(bw - px * 2, 10); rh = max(bh - py * 2, 10)
    roi = frame[ry:ry + rh, rx:rx + rw]
    if roi.size == 0: roi = frame[y:y + bh, x:x + bw]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [36, 48], [0, 180, 0, 256])
    cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
    return hist

def colour_score(frame, tx, ty, tw, th, sig):
    tx, ty = max(0, tx), max(0, ty)
    tw = min(tw, frame.shape[1] - tx); th = min(th, frame.shape[0] - ty)
    if tw < 5 or th < 5: return 0.0
    roi = frame[ty:ty + th, tx:tx + tw]
    if roi.size == 0: return 0.0
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [36, 48], [0, 180, 0, 256])
    cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
    return float(cv2.compareHist(sig, hist, cv2.HISTCMP_CORREL))

def get_team_hue(frame, x, y, bw, bh):
    roi = frame[max(0, y):min(y + bh, frame.shape[0]), max(0, x):min(x + bw, frame.shape[1])]
    if roi.size == 0: return 60
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    sat_mask = hsv[:, :, 1] > 60
    if sat_mask.sum() < 10: return 60
    hues = hsv[:, :, 0][sat_mask]
    hist, _ = np.histogram(hues, bins=18, range=(0, 180))
    return int(hist.argmax() * 10)

def hue_dist(h1, h2):
    d = abs(h1 - h2); return min(d, 180 - d)

def detect_ball(frame, pitch_hue):
    try:
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        m1 = cv2.inRange(hsv, np.array([0, 0, 170]), np.array([180, 60, 255]))
        m2 = cv2.inRange(hsv, np.array([0, 100, 100]), np.array([30, 255, 255]))
        mask = m1 | m2
        pm = cv2.inRange(hsv, np.array([max(0, pitch_hue - 25), 40, 40]), np.array([min(180, pitch_hue + 25), 255, 255]))
        mask = cv2.bitwise_and(mask, cv2.bitwise_not(pm))
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k), cv2.MORPH_OPEN, k)
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best, best_s = None, 0
        for c in cnts:
            area = cv2.contourArea(c)
            if area < 60 or area > 5000: continue
            perim = cv2.arcLength(c, True)
            if perim == 0: continue
            circ = 4 * math.pi * area / (perim * perim)
            if circ > 0.6 and area > best_s:
                best_s = area
                M = cv2.moments(c)
                if M['m00'] > 0:
                    best = (int(M['m10'] / M['m00']), int(M['m01'] / M['m00']))
        return best if best else (None, None)
    except:
        return None, None

class TrajectoryPredictor:
    def __init__(self): self.hist = []
    def update(self, cx, cy, fn):
        self.hist.append((fn, cx, cy))
        if len(self.hist) > 10: self.hist.pop(0)
    def predict(self, steps=1):
        if not self.hist: return None, None
        if len(self.hist) < 2: return self.hist[-1][1], self.hist[-1][2]
        frames = np.array([h[0] for h in self.hist], dtype=float)
        xs = np.array([h[1] for h in self.hist], dtype=float)
        ys = np.array([h[2] for h in self.hist], dtype=float)
        vx = np.polyfit(frames, xs, 1)[0]; vy = np.polyfit(frames, ys, 1)[0]
        return int(xs[-1] + vx * steps), int(ys[-1] + vy * steps)

def reid_player(frame, sig, team_hue, predictor, bw, bh):
    h, w = frame.shape[:2]
    px, py = predictor.predict(steps=3)
    if px is None: px, py = w // 2, h // 2
    best_score = 0.45; best_bbox = None
    sx, sy = max(bw // 2, 20), max(bh // 2, 20)
    for radius in [(bw * 3, bh * 3), (bw * 8, bh * 8), (w, h)]:
        x1 = max(0, px - radius[0]); x2 = min(w - bw, px + radius[0])
        y1 = max(0, py - radius[1]); y2 = min(h - bh, py + radius[1])
        for fy in range(y1, y2, sy):
            for fx in range(x1, x2, sx):
                if hue_dist(get_team_hue(frame, fx, fy, bw, bh), team_hue) > 35: continue
                score = colour_score(frame, fx, fy, bw, bh, sig)
                dist = math.sqrt((fx + bw // 2 - px) ** 2 + (fy + bh // 2 - py) ** 2)
                score += 0.1 * (1 - dist / max(math.sqrt(radius[0] ** 2 + radius[1] ** 2), 1))
                if score > best_score: best_score = score; best_bbox = (fx, fy, bw, bh)
        if best_bbox: break
    return best_bbox

def make_tracker():
    for fn in [lambda: cv2.TrackerCSRT_create(), lambda: cv2.legacy.TrackerCSRT_create(),
               lambda: cv2.TrackerKCF_create(), lambda: cv2.legacy.TrackerKCF_create(),
               lambda: cv2.TrackerMIL_create(), lambda: cv2.legacy.TrackerMIL_create()]:
        try:
            t = fn()
            if t: return t
        except: pass
    return None

def run_job(jid, filepath, bbox, player_info, prev_goals):
    try:
        update_job(jid, {'step': 'Opening video', 'progress': 2})
        cap = cv2.VideoCapture(filepath)
        if not cap.isOpened(): set_job(jid, {'status': 'error', 'error': 'Cannot open video'}); return
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        ret, frame0 = cap.read()
        if not ret: set_job(jid, {'status': 'error', 'error': 'Cannot read frame'}); cap.release(); return
        h0, w0 = frame0.shape[:2]
        scale = min(1.0, 1280 / w0)
        if scale < 1.0: frame0 = cv2.resize(frame0, (int(w0 * scale), int(h0 * scale)))
        x = max(0, int(bbox['x'])); y = max(0, int(bbox['y']))
        bw = min(int(bbox['w']), frame0.shape[1] - x)
        bh = min(int(bbox['h']), frame0.shape[0] - y)
        player_h = bh
        sig = build_sig(frame0, x, y, bw, bh)
        team_hue = get_team_hue(frame0, x, y, bw, bh)
        ph = frame0.shape[0]; pitch_hue = 60
        for corner in [frame0[ph - 30:ph - 10, 10:80], frame0[10:40, 10:80]]:
            if corner.size > 0:
                hsv_c = cv2.cvtColor(corner, cv2.COLOR_BGR2HSV)
                pitch_hue = int(np.mean(hsv_c[:, :, 0])); break
        tracker = make_tracker()
        if tracker is None: set_job(jid, {'status': 'error', 'error': 'No tracker'}); cap.release(); return
        tracker.init(frame0, (x, y, bw, bh))
        predictor = TrajectoryPredictor(); predictor.update(x + bw // 2, y + bh // 2, 0)
        sample_every = max(1, int(fps / 6)); LOST_THRESH = int(fps * 3 / sample_every)
        positions = []; ball_pos = []; frame_num = 0; lost_count = 0
        prev_cx = x + bw // 2; prev_cy = y + bh // 2; time_on_ball = 0.0
        update_job(jid, {'step': 'Tracking player…', 'progress': 12})
        while True:
            ret, frame = cap.read()
            if not ret: break
            frame_num += 1
            if scale < 1.0: frame = cv2.resize(frame, (int(w0 * scale), int(h0 * scale)))
            if frame_num % sample_every != 0: continue
            bx, by = detect_ball(frame, pitch_hue)
            if bx is not None: ball_pos.append((frame_num, bx, by))
            ok, tb = tracker.update(frame)
            if ok:
                tx, ty, tw, th = int(tb[0]), int(tb[1]), int(tb[2]), int(tb[3])
                cx, cy = tx + tw // 2, ty + th // 2
                conf = colour_score(frame, tx, ty, tw, th, sig)
                wrong_team = hue_dist(get_team_hue(frame, tx, ty, tw, th), team_hue) > 35
                if conf > 0.30 and not wrong_team:
                    if bx is not None and math.sqrt((cx - bx) ** 2 + (cy - by) ** 2) < player_h * 1.5:
                        time_on_ball += sample_every / fps
                    positions.append((frame_num, cx, cy, conf))
                    predictor.update(cx, cy, frame_num); prev_cx, prev_cy = cx, cy; lost_count = 0
                else:
                    lost_count += 1
            else:
                lost_count += 1
            if lost_count >= LOST_THRESH:
                nb = reid_player(frame, sig, team_hue, predictor, bw, bh)
                if nb:
                    tracker = make_tracker(); tracker.init(frame, nb)
                    ncx, ncy = nb[0] + nb[2] // 2, nb[1] + nb[3] // 2
                    predictor.update(ncx, ncy, frame_num); prev_cx, prev_cy = ncx, ncy; lost_count = 0
                elif lost_count > LOST_THRESH * 4:
                    lost_count = LOST_THRESH
            if frame_num % (sample_every * 15) == 0:
                pct = int(12 + (frame_num / max(total, 1)) * 73)
                update_job(jid, {'step': 'Tracking {}/{}'.format(frame_num, total), 'progress': min(pct, 85)})
        cap.release()
        update_job(jid, {'step': 'Computing stats', 'progress': 88})
        stats = compute_stats(positions, ball_pos, fps, player_h, total, time_on_ball)
        update_job(jid, {'step': 'Generating feedback', 'progress': 95})
        feedback = generate_feedback(stats, player_info, prev_goals)
        set_job(jid, {'status': 'done', 'progress': 100, 'step': 'Complete', 'stats': stats, 'feedback': feedback})
    except Exception as e:
        set_job(jid, {'status': 'error', 'error': str(e), 'detail': traceback.format_exc()})

def compute_stats(positions, ball_pos, fps, player_h, total_frames, time_on_ball):
    if len(positions) < 2: return default_stats()
    px_per_m = max(player_h / 1.75, 1)
    SPRINT = 3.5; RUN = 2.0; JOG = 1.0; IDLE = 0.3; BALL_R = player_h * 1.5
    ball_lookup = {}
    for fn, bx, by in ball_pos: ball_lookup[int(fn)] = (bx, by)
    speeds = []
    for i in range(1, len(positions)):
        f1, x1, y1, c1 = positions[i - 1]; f2, x2, y2, c2 = positions[i]
        dt = (f2 - f1) / fps
        if dt <= 0: continue
        dm = float(np.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)) / px_per_m
        speeds.append([float(f2), dm / dt, dm, float(x2), float(y2), (c1 + c2) / 2])
    if not speeds: return default_stats()
    win = 3; smoothed = []
    for i in range(len(speeds)):
        lo, hi = max(0, i - win), min(len(speeds), i + win + 1)
        avg_s = float(np.mean([s[1] for s in speeds[lo:hi]]))
        smoothed.append([speeds[i][0], avg_s, speeds[i][2], speeds[i][3], speeds[i][4], speeds[i][5]])
    total_m = sprint_m = 0.0; sprints = 0; in_sprint = False
    passes = shots = tackles = dribbles = 0; prev_s = 0.0; stop_flag = False; decel_str = 0
    for i, (fn, spd, dm, cx, cy, conf) in enumerate(smoothed):
        if conf < 0.25: continue
        total_m += dm
        if spd > SPRINT:
            sprint_m += dm
            if not in_sprint: in_sprint = True; sprints += 1
        else:
            in_sprint = False
        ball_near = any(math.sqrt((cx - bp[0]) ** 2 + (cy - bp[1]) ** 2) < BALL_R
                        for df in range(-3, 4) for bp in [ball_lookup.get(int(fn) + df)] if bp)
        decel = prev_s - spd
        if decel > 1.5 and prev_s > RUN: decel_str += 1; stop_flag = True
        elif spd < IDLE and stop_flag:
            if not ball_near: tackles += 1
            stop_flag = False; decel_str = 0
        elif spd > RUN: stop_flag = False; decel_str = 0
        prev_s = spd
        if ball_near and i >= 2:
            s0 = smoothed[i - 2][1]; s1 = smoothed[i - 1][1]
            x0, y0 = smoothed[i - 2][3], smoothed[i - 2][4]
            x1v, y1v = smoothed[i - 1][3], smoothed[i - 1][4]
            if spd < s1 * 0.6 and s1 > s0 * 1.5:
                v1 = np.array([x1v - x0, y1v - y0]); v2 = np.array([cx - x1v, cy - y1v])
                n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
                if n1 > 0 and n2 > 0 and float(np.dot(v1, v2)) / (n1 * n2) < 0.1: passes += 1
            if s1 > SPRINT * 1.2 and spd < JOG and s0 < SPRINT: shots += 1
            if spd > RUN and s1 > RUN and s0 > RUN: dribbles += 1
    minutes = round((total_frames / fps) / 60, 1) if fps > 0 else 90.0
    return {'passes': max(int(passes), 0), 'shots': max(int(shots), 0),
            'shotsOnTarget': max(int(shots * 0.6), 0), 'tackles': max(int(tackles), 0),
            'dribbles': max(int(dribbles // 3), 0), 'sprints': max(int(sprints), 0),
            'sprintMeters': round(sprint_m, 1), 'metersRan': round(total_m, 1),
            'timeOnBall': round(time_on_ball, 1), 'chancesCreated': 0,
            'goals': 0, 'assists': 0, 'minutesPlayed': minutes,
            'performanceScore': max(20, min(99, int(50 + min(passes * 0.8, 15) + min(shots * 3, 10) + min(tackles * 2, 10) + min(dribbles * 2, 8) + min(sprint_m / 50, 7)))),
            'trackingPoints': len(positions), 'ballDetections': len(ball_pos)}

def default_stats():
    return {'passes': 0, 'shots': 0, 'shotsOnTarget': 0, 'tackles': 0, 'dribbles': 0,
            'sprints': 0, 'sprintMeters': 0, 'metersRan': 0, 'timeOnBall': 0,
            'chancesCreated': 0, 'goals': 0, 'assists': 0, 'minutesPlayed': 0,
            'performanceScore': 0, 'trackingPoints': 0, 'ballDetections': 0}

def generate_feedback(stats, player_info, prev_goals):
    name = player_info.get('firstName', 'Player')
    s = stats; score = s['performanceScore']
    grade = 'excellent' if score >= 80 else 'solid' if score >= 65 else 'mixed'
    text = ('Overall this was a {} performance from {}. Tracking recorded {} position samples across {} minutes.\n\n'
            '{} covered {}m total with {}m at sprint pace across {} sprints. Time on ball: {}s.\n\n'
            '{} passes, {} shots ({} on target), {} tackles, {} dribbles recorded.'
            ).format(grade, name, s['trackingPoints'], s['minutesPlayed'],
                     name, s['metersRan'], s['sprintMeters'], s['sprints'], s['timeOnBall'],
                     s['passes'], s['shots'], s['shotsOnTarget'], s['tackles'], s['dribbles'])
    prev_review = 'Goals review: {}. {}'.format(prev_goals, 'Good progress.' if score >= 65 else 'More work needed.') if prev_goals else ''
    goals = [
        {'title': 'Passing Volume', 'target': 'Complete {}+ passes'.format(max(s['passes'] + 5, 20)), 'reason': 'Higher pass count means more involvement in play'},
        {'title': 'Sprint Distance', 'target': 'Cover {}m+ at sprint pace'.format(max(int(s['sprintMeters'] * 1.2), 300)), 'reason': 'Sprint output impacts your ability to create and press'},
        {'title': 'Shots on Target', 'target': '{} shots on target'.format(max(s['shotsOnTarget'] + 1, 2)), 'reason': 'Quality finishing starts with putting the ball on target'},
        {'title': 'Tackle Count', 'target': '{} tackles'.format(max(s['tackles'] + 1, 3)), 'reason': 'Defensive contribution is key to team performance'},
    ]
    return {'text': text, 'prevGoalReview': prev_review, 'nextGoals': goals}


# ── STARTUP ──
with app.app_context():
    init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)


