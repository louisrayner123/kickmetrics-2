# KickMetrics – Football Performance AI

## Deployment Steps

### 1. Install Python
https://python.org/downloads — tick "Add Python to PATH"

### 2. Test locally
cd kickmetrics
pip install -r requirements.txt
python app.py
Open: http://localhost:8080

### 3. Deploy to Railway
1. Push all files to a GitHub repo called "kickmetrics"
2. Go to railway.app → New Project → Deploy from GitHub
3. Select your repo — Railway auto-detects Python
4. Add environment variable: SECRET_KEY = any-random-string
5. Click Generate Domain → your site is live

## How It Works

### Coach Flow
1. Sign up at / → create team → get invite link
2. Share invite link or team code with players
3. Dashboard shows squad stats, rankings, set goals

### Player Flow  
1. Click invite link → sign up with team code
2. Personal dashboard shows stats and coach goals
3. Upload match video → select yourself → get analysis

### Customisation
- Coaches: Upload team logo → primary colour extracted automatically
- Players: Pick their own dashboard colour from 10 options

## Platform Structure
/ → Landing page (signup/login)
/coach → Coach dashboard
/player → Player dashboard  
/analysis → Video analysis room
/join/[CODE] → Player invite link
