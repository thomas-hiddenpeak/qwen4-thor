"""CPU-only prospective decision geometry; no model/HTTP/GPU invocation."""
import json, hashlib, math
from pathlib import Path
import numpy as np
from scipy.stats import norm, nct, chi2
from scipy.optimize import brentq
OUT=Path(__file__).resolve().parent
K=float(norm.ppf(.975)*math.sqrt(2))
PRECISION_CAP=math.log1p(.002)

def layout(mode,n=5):
    # Fixed hash ranking before any new observations, 3 AB / 2 BA for n=5.
    rank=sorted(range(n),key=lambda j:hashlib.sha256(f'b1a-20261018-final-{mode}-{n}-{j}'.encode()).digest())
    ab=set(rank[:(n+1)//2]);seq=[]
    for j in range(n): seq+=['A','B'] if j in ab else ['B','A']
    t=np.arange(2*n,dtype=float);t-=t.mean()
    z=np.array([v=='B' for v in seq],float)
    X=np.column_stack([np.ones(2*n),t,z]);P=np.linalg.pinv(X)
    a=z==0;XA=X[a,:2];PA=np.linalg.pinv(XA)
    residual=np.eye(n)-XA@PA
    assert np.max(np.abs(P[2,a]@residual))<1e-12
    q=float((P@P.T)[2,2]);df=n-2
    return seq,X,P,a,residual,q,df

def interval(t,q,df):
    def root(p):
        lo,hi=-1.,1.
        while nct.cdf(t,df,lo)<p:lo*=2
        while nct.cdf(t,df,hi)>p:hi*=2
        nc=brentq(lambda nc:nct.cdf(t,df,nc)-p,lo,hi)
        assert abs(nct.cdf(t,df,nc)-p)<1e-9
        return float(nc*math.sqrt(q))
    return {'upper_95':root(.05),'lower_99_75':root(.9975)}

def decision(y,mode):
    seq,X,P,a,R,q,df=layout(mode)
    y=np.asarray(y);theta=float((P@y)[2]);s=float(np.linalg.norm(R@y[a])/math.sqrt(df))
    if not s>0:return {'decision':'INVALID_ZERO_SCALE'}
    t=theta/(s*math.sqrt(q));ci=interval(t,q,df)
    reject=ci['lower_99_75']>0
    band_upper=K*s*math.sqrt(df/chi2.ppf(.05,df))
    accept=ci['upper_95']<=K and band_upper<=PRECISION_CAP and not reject
    return {'theta':theta,'baseline_sigma':s,'q':q,'df':df,'eta_interval':ci,
            'band_log_estimate':K*s,'band_upper_95':band_upper,'precision_cap_log':PRECISION_CAP,
            'band_log_pointwise_95':[K*s*math.sqrt(df/chi2.ppf(.975,df)),K*s*math.sqrt(df/chi2.ppf(.025,df))],
            'decision':'NO_GO' if reject else 'GO_ITEM' if accept else 'UNRESOLVED'}

rng=np.random.default_rng(20261018);rows=[]
for n in [3,4,5,6,8]:
    _,X,P,a,R,q,df=layout('off',n)
    cutoff=float(nct.ppf(.05,df,K/math.sqrt(q)))
    p=float(nct.cdf(cutoff,df,0))
    rows.append({'starts_each_identity_per_mode':n,'requests':60*n,'df':df,'q':q,
                 'flat_item_pass_before_other_gates':p,'independent_20_items':p**20,
                 'five_sigma_regression_detection':float(nct.sf(nct.ppf(.9975,df,0),df,5/math.sqrt(q)))})
checks={};schedule=[]
for mode in ['off','on']:
    seq,X,P,a,R,q,df=layout(mode)
    for j,side in enumerate(seq):schedule.append({'slot':len(schedule),'mode':mode,'binary':side,'requests':15,'start_offset_seconds':1080*len(schedule)})
    # Synthetic outcomes illustrate reachable decisions; not actual performance.
    e=rng.normal(size=10)*1e-4
    e=e-X@(P@e) # make zero fitted treatment without zero A residual
    base=X@np.array([0.,2e-5,0.])+e
    flat=decision(base,mode);bad=decision(base+X[:,2]*.01,mode)
    assert flat['decision']=='GO_ITEM' and bad['decision']=='NO_GO'
    # Exact model Monte Carlo verifies baseline residual/effect independence and
    # boundary false-GO control without pretending assumptions fit real data.
    E=rng.normal(size=(200000,10));theta=E@P[2]
    scale=np.linalg.norm(E[:,a]@R.T,axis=1)/math.sqrt(df)
    stat=(theta+K)/(scale*math.sqrt(q))
    err=float(np.mean(stat<nct.ppf(.05,df,K/math.sqrt(q))))
    assert .047<err<.053
    checks[mode]={'sequence':seq,'q':q,'df':df,'synthetic_flat':flat,'synthetic_regression':bad,'boundary_false_go_mc':err}
precision_scenarios=[]
_,_,_,_,_,q,df=layout('off')
z=rng.normal(size=(100000,20));w=np.sqrt(rng.chisquare(df,size=(100000,20))/df)
for one_noisy in [False,True]:
    sigmas=np.full(20,.0001)
    if one_noisy:sigmas[0]=.001
    scale=w*sigmas
    tt=z/w
    item=(tt<nct.ppf(.05,df,K/math.sqrt(q))) & (tt<=nct.ppf(.9975,df,0)) & (K*scale*math.sqrt(df/chi2.ppf(.05,df))<=PRECISION_CAP)
    precision_scenarios.append({'true_log_sigma':[float(v) for v in sigmas],'model_gates_not_included':True,'flat_global_go_mc':float(np.mean(np.all(item,axis=1)))})
value={'scope':'synthetic model mathematics only, zero HTTP/GPU requests','band_standardized':K,
       'precision_cap_log':PRECISION_CAP,'precision_scenarios':precision_scenarios,'candidate_sizes':rows,'schedule':schedule,'checks':checks,'caps':{'requests':300,'starts':20,'wall_seconds':25200,'retries':0},
       'assumptions':'normal independent equal-variance service errors; common linear trend per mode; no carryover',
       'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
(OUT/'design-check.json').write_text(json.dumps(value,indent=2)+'\n')
print(json.dumps({'candidate_sizes':rows,'checks':{m:{k:v for k,v in x.items() if k not in ['synthetic_flat','synthetic_regression']} for m,x in checks.items()}},indent=2))
