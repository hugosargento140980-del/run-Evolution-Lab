import os, json, math, secrets
from datetime import datetime
from functools import wraps
from flask import Flask, request, session, redirect, url_for, render_template, flash, jsonify, abort
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash

app=Flask(__name__)
app.config.update(
 SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
 SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL","sqlite:///run_evolution.db").replace("postgres://","postgresql://",1),
 SQLALCHEMY_TRACK_MODIFICATIONS=False, SESSION_COOKIE_HTTPONLY=True,
 SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE","1")=="1")
db=SQLAlchemy(app); csrf=CSRFProtect(app)
limiter=Limiter(key_func=get_remote_address,app=app,default_limits=["200 per hour"])

class User(db.Model):
 id=db.Column(db.Integer,primary_key=True); name=db.Column(db.String(120),nullable=False)
 email=db.Column(db.String(255),unique=True,nullable=False,index=True); password_hash=db.Column(db.String(255),nullable=False)
 role=db.Column(db.String(20),default="athlete",nullable=False); status=db.Column(db.String(20),default="pending",nullable=False)
 created_at=db.Column(db.DateTime,default=datetime.utcnow)
class Profile(db.Model):
 user_id=db.Column(db.Integer,db.ForeignKey("user.id"),primary_key=True)
 height=db.Column(db.Float); weight=db.Column(db.Float); resting_hr=db.Column(db.Integer); max_hr=db.Column(db.Integer)
 vo2max=db.Column(db.Float); lt1_pace=db.Column(db.String(20)); lt1_hr=db.Column(db.Integer); lt2_pace=db.Column(db.String(20)); lt2_hr=db.Column(db.Integer); vlamax=db.Column(db.Float); goal=db.Column(db.String(255))
class Training(db.Model):
 id=db.Column(db.Integer,primary_key=True); user_id=db.Column(db.Integer,index=True,nullable=False); date=db.Column(db.String(20)); type=db.Column(db.String(40))
 distance=db.Column(db.Float); duration=db.Column(db.String(30)); pace=db.Column(db.String(20)); avg_hr=db.Column(db.Integer); max_hr=db.Column(db.Integer)
 power=db.Column(db.Float); cadence=db.Column(db.Float); lactate=db.Column(db.Float); rpe=db.Column(db.Float); notes=db.Column(db.Text)
class Test(db.Model):
 id=db.Column(db.Integer,primary_key=True); user_id=db.Column(db.Integer,index=True,nullable=False); test_date=db.Column(db.String(20))
 protocol=db.Column(db.String(120)); result_json=db.Column(db.Text)
class Stage(db.Model):
 id=db.Column(db.Integer,primary_key=True); test_id=db.Column(db.Integer,index=True,nullable=False); stage_no=db.Column(db.Integer)
 duration=db.Column(db.String(30)); pace=db.Column(db.String(20)); speed=db.Column(db.Float); heart_rate=db.Column(db.Integer); lactate=db.Column(db.Float)

def current(): return User.query.get(session.get("uid")) if session.get("uid") else None
def login_required(f):
 @wraps(f)
 def w(*a,**k):
  u=current()
  if not u or u.status!="approved": session.clear(); return redirect(url_for("login"))
  return f(*a,**k)
 return w
def supervisor_required(f):
 @wraps(f)
 def w(*a,**k):
  u=current()
  if not u or u.role!="supervisor": abort(403)
  return f(*a,**k)
 return w

def pace_sec(p):
 try:
  a=p.split(":"); return int(a[0])*60+float(a[1]) if len(a)==2 else float(p)*60
 except: return None
def pace_from_speed(v):
 if not v or v<=0:return None
 s=3600/v; return f"{int(s//60)}:{int(round(s%60)):02d}/km"
def speed_from_pace(p):
 s=pace_sec(p); return 3600/s if s and s>0 else None
def lin_interp(x1,y1,x2,y2,y):
 if x2==x1:return x1
 return x1+(y-y1)*(x2-x1)/(y2-y1)

def analyze_lactate(stages, max_hr=None):
 """Scientific-oriented field analysis.
    LT1: first sustained rise above baseline using +0.5 mmol/L and/or 2.0 anchor.
    LT2: Dmax between baseline-to-end lactate curve, with 4 mmol anchor as corroboration.
    VO2max: field estimate from maximal running speed only; NOT a laboratory VO2 measurement.
    All outputs include method and confidence.
 """
 a=[]
 for s in stages:
  speed=float(s.get("speed") or speed_from_pace(s.get("pace","")) or 0)
  lac=float(s.get("lactate") or 0); hr=int(s.get("hr") or 0)
  if speed>0 and lac>=0:a.append({"speed":speed,"lactate":lac,"hr":hr,"pace":s.get("pace") or pace_from_speed(speed),"duration":s.get("duration","")})
 a.sort(key=lambda x:x["speed"])
 if len(a)<4:return {"error":"São necessários pelo menos 4 estágios válidos."}
 for i,x in enumerate(a):
  vals=[a[j]["lactate"] for j in range(max(0,i-1),min(len(a),i+2))]
  x["smooth"]=sum(vals)/len(vals)
 baseline=sum(x["smooth"] for x in a[:min(2,len(a))])/min(2,len(a))
 lt1i=None
 for i,x in enumerate(a):
  support=a[i+1]["smooth"] if i+1<len(a) else x["smooth"]
  if x["smooth"]>=baseline+0.5 and support>=x["smooth"]-0.15:
   lt1i=i; break
 if lt1i is None: lt1i=min(range(len(a)),key=lambda i:abs(a[i]["smooth"]-2.0))
 x1,y1=a[0]["speed"],a[0]["smooth"]; x2,y2=a[-1]["speed"],a[-1]["smooth"]
 den=math.hypot(y2-y1,x2-x1) or 1
 d=[]
 for i,x in enumerate(a):
  dist=abs((y2-y1)*x["speed"]-(x2-x1)*x["smooth"]+x2*y1-y2*x1)/den
  d.append(dist)
 lt2i=max(range(1,len(a)-1),key=lambda i:d[i]) if len(a)>2 else len(a)-1
 anchor4=next((i for i,x in enumerate(a) if x["smooth"]>=4),None)
 vo2_est=3.5 + 0.2*(a[-1]["speed"]*60/60)*3.5
 result={
  "lt1":{"speed":round(a[lt1i]["speed"],3),"pace":pace_from_speed(a[lt1i]["speed"]),"hr":a[lt1i]["hr"],"lactate":round(a[lt1i]["smooth"],2)},
  "lt2":{"speed":round(a[lt2i]["speed"],3),"pace":pace_from_speed(a[lt2i]["speed"]),"hr":a[lt2i]["hr"],"lactate":round(a[lt2i]["smooth"],2)},
  "vo2max_est":round(vo2_est,1),
  "max_speed":round(a[-1]["speed"],3),"max_pace":pace_from_speed(a[-1]["speed"]),
  "anchor_4mmol_stage":anchor4+1 if anchor4 is not None else None,
  "baseline_lactate":round(baseline,2),
  "method":{"lt1":"baseline +0.5 mmol/L sustained rise, corroborated against ~2 mmol/L",
            "lt2":"Dmax curve method, with 4 mmol/L anchor reported separately",
            "vo2max":"field estimate from maximal speed; not direct VO2 measurement"},
  "confidence":{"lt1":"moderate" if len(a)>=5 else "low","lt2":"moderate" if len(a)>=6 else "low","vo2max":"low without gas exchange"},
  "curve":[{"speed":round(x["speed"],3),"pace":x["pace"],"hr":x["hr"],"lactate":round(x["lactate"],2),"smooth":round(x["smooth"],2)} for x in a]
 }
 return result

@app.route("/")
def home(): return redirect(url_for("dashboard") if current() else url_for("login"))
@app.route("/login",methods=["GET","POST"])
@limiter.limit("10 per minute")
def login():
 if request.method=="POST":
  u=User.query.filter_by(email=request.form["email"].strip().lower()).first()
  if u and check_password_hash(u.password_hash,request.form["password"]) and u.status=="approved":
   session.clear();session["uid"]=u.id;return redirect(url_for("dashboard"))
  flash("Credenciais inválidas ou conta não aprovada.")
 return render_template("login.html")
@app.route("/register",methods=["GET","POST"])
@limiter.limit("5 per hour")
def register():
 if request.method=="POST":
  n=request.form["name"].strip();e=request.form["email"].strip().lower();pw=request.form["password"]
  if not n or not e or len(pw)<10:flash("Preencha os campos. Password mínima: 10 caracteres.")
  elif User.query.filter_by(email=e).first():flash("Email já registado.")
  else:
   db.session.add(User(name=n,email=e,password_hash=generate_password_hash(pw)));db.session.commit();flash("Pedido enviado.");return redirect(url_for("login"))
 return render_template("register.html")
@app.route("/logout")
def logout():session.clear();return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
 u=current();p=Profile.query.get(u.id); athletes=User.query.filter_by(role="athlete").order_by(User.created_at.desc()).all() if u.role=="supervisor" else []
 trainings=Training.query.filter_by(user_id=u.id).order_by(Training.id.desc()).limit(50).all()
 tests_raw=Test.query.filter_by(user_id=u.id).order_by(Test.id.desc()).all()
 tests=[]
 for t in tests_raw:
  try:
   r=json.loads(t.result_json) if t.result_json else {}
  except Exception:
   r={}
  tests.append({
   "id":t.id,"test_date":t.test_date,
   "lt1_pace":(r.get("lt1") or {}).get("pace"),
   "lt2_pace":(r.get("lt2") or {}).get("pace"),
   "vo2max_est":r.get("vo2max_est"),
  })
 return render_template("dashboard.html",user=u,profile=p,athletes=athletes,trainings=trainings,tests=tests)

@app.post("/admin/user/<int:uid>/<action>")
@supervisor_required
def user_action(uid,action):
 u=User.query.get_or_404(uid)
 if u.role=="athlete" and action in {"approve","block","reactivate"}:u.status={"approve":"approved","block":"blocked","reactivate":"approved"}[action];db.session.commit()
 return redirect(url_for("dashboard"))

@app.post("/profile")
@login_required
def profile_save():
 u=current();p=Profile.query.get(u.id) or Profile(user_id=u.id);db.session.add(p)
 for f in ["height","weight","resting_hr","max_hr","vo2max","lt1_pace","lt1_hr","lt2_pace","lt2_hr","vlamax","goal"]:setattr(p,f,request.form.get(f) or None)
 db.session.commit();return redirect(url_for("dashboard"))

@app.post("/training")
@login_required
def training_save():
 f=request.form;db.session.add(Training(user_id=current().id,date=f.get("date"),type=f.get("type"),distance=f.get("distance") or None,duration=f.get("duration"),pace=f.get("pace"),avg_hr=f.get("avg_hr") or None,max_hr=f.get("max_hr") or None,power=f.get("power") or None,cadence=f.get("cadence") or None,lactate=f.get("lactate") or None,rpe=f.get("rpe") or None,notes=f.get("notes")));db.session.commit();return redirect(url_for("dashboard"))

@app.post("/test")
@login_required
def test_save():
 raw=request.get_json(silent=True) or {};result=analyze_lactate(raw.get("stages",[]),raw.get("max_hr"))
 if "error" in result:return jsonify(error=result["error"]),400
 t=Test(user_id=current().id,test_date=raw.get("date"),protocol=raw.get("protocol"),result_json=json.dumps(result));db.session.add(t);db.session.flush()
 for i,s in enumerate(result["curve"],1):db.session.add(Stage(test_id=t.id,stage_no=i,duration="",pace=s["pace"],speed=s["speed"],heart_rate=s["hr"] or None,lactate=s["lactate"]))
 db.session.commit();return jsonify(result=result)

@app.get("/api/my-data")
@login_required
def my_data():
 u=current();p=Profile.query.get(u.id);ts=Training.query.filter_by(user_id=u.id).all();tests=Test.query.filter_by(user_id=u.id).all()
 return jsonify(profile={c.name:getattr(p,c.name) for c in Profile.__table__.columns} if p else None,
 trainings=[{c.name:getattr(t,c.name) for c in Training.__table__.columns} for t in ts],
 tests=[{"id":t.id,"date":t.test_date,"result":json.loads(t.result_json)} for t in tests])

with app.app_context():
 db.create_all()
 if not User.query.filter_by(email="supervisor@runlab.local").first():
  db.session.add(User(name="Supervisor",email="supervisor@runlab.local",password_hash=generate_password_hash(os.environ.get("INITIAL_SUPERVISOR_PASSWORD","ChangeMe123!")),role="supervisor",status="approved"));db.session.commit()
if __name__=="__main__":app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)))
