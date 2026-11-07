#!/usr/bin/env python3
"""Compare MPA GeoSpace-Sea layers (downloaded with scripts/download_gss.py) with the model grid.

Usage:  python scripts/compare_gss.py [data/gss]
Prints: depth comparison (DepthsA vs GEBCO+dredged pockets), chart-cell coverage, traffic
lanes, coastline, hazards (DangersP) and named berths. Data are not for navigation.
"""
import json, sys, collections, os
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from shapely.geometry import shape, box
from shapely import contains_xy, STRtree
from shapely.ops import unary_union
from saferoute import cache
ROOT = sys.argv[1] if len(sys.argv) > 1 else "data/gss"
G = ROOT + "/%s/%s.geojson"
def load(n, name):
    return [(shape(f["geometry"]), f["properties"]) for f in json.load(open(G % (n, name)))["features"] if f.get("geometry")]
g = cache.grid()
lats, lons = g.lats, g.lons
print("grid", g.shape, "bbox", lats[0], lats[-1], lons[0], lons[-1], "| depth source:", g.depth_source)
bb = box(lons[0], lats[0], lons[-1], lats[-1])

# ---------- DepthsA coverage & compare with grid depth ----------
dA = load('depthsa','DepthsA')
print("\nDEPTHS A: features", len(dA), "in bbox:", sum(bb.intersects(s) for s,_ in dA))
vd = collections.Counter(p['VERDAT_TXT'] for _,p in dA); print(" vertical datum:", dict(vd))
LON, LAT = np.meshgrid(lons, lats)
dr1 = np.full(g.shape, np.nan); dr2 = np.full(g.shape, np.nan)
polys = [s for s,_ in dA]; props = [p for _,p in dA]
tree = STRtree(polys)
# rasterise: for each polygon, mark cells whose centre is inside; shallowest DRVAL1 wins
order = np.argsort([-(p['DRVAL1'] if p['DRVAL1'] is not None else 0) for p in props])  # deep first, shallow overwrites
for i in order:
    s, p = polys[i], props[i]
    minx, miny, maxx, maxy = s.bounds
    if maxx < lons[0] or minx > lons[-1] or maxy < lats[0] or miny > lats[-1]: continue
    ix0 = max(int((minx-lons[0])/g.resolution_deg)-1,0); ix1 = min(int((maxx-lons[0])/g.resolution_deg)+2, g.nx)
    iy0 = max(int((miny-lats[0])/g.resolution_deg)-1,0); iy1 = min(int((maxy-lats[0])/g.resolution_deg)+2, g.ny)
    if ix1<=ix0 or iy1<=iy0: continue
    sub = contains_xy(s, LON[iy0:iy1, ix0:ix1], LAT[iy0:iy1, ix0:ix1])
    dr1[iy0:iy1, ix0:ix1][sub] = p['DRVAL1']; dr2[iy0:iy1, ix0:ix1][sub] = p['DRVAL2']
cov = ~np.isnan(dr1)
print(" cells covered:", int(cov.sum()), "of", g.land.size, "(%.0f%% of grid, %.0f%% of sea cells)" % (100*cov.mean(), 100*(cov & ~g.land).sum()/(~g.land).sum()))
sea = cov & ~g.land & (dr2 > 0)
mid = (dr1 + dr2)/2
for name, arr in (("model depth (GEBCO+pockets)", g.depth_m),):
    e = arr[sea] - mid[sea]
    print(" %s vs DepthsA mid-range: n=%d MAE=%.1f m bias=%+.1f m" % (name, sea.sum(), np.abs(e).mean(), e.mean()))
    # lower bound check: model deeper than charted shallowest?
    over = (arr[sea] > dr2[sea] + 0.5)
    print("   model deeper than DepthsA's DEEPEST value (DRVAL2) in %.1f%% of cells; shallower than DRVAL1 in %.1f%%" % (100*over.mean(), 100*(arr[sea] < dr1[sea]-0.5).mean()))
# cells where model says >=15 m (navigable for Panamax) but chart says <15
m15 = sea & (g.depth_m >= 15)
print(" model >=15 m cells: %d; charted deepest <15 m in %d (%.1f%%); charted shallowest <10 m in %d" % (m15.sum(), (m15 & (dr2<15)).sum(), 100*(m15&(dr2<15)).sum()/max(m15.sum(),1), (m15 & (dr1<10)).sum()))
cm15 = sea & (dr1 >= 15)
print(" charted >=15 m everywhere (DRVAL1>=15): %d cells; model <15 in %d (%.1f%%)" % (cm15.sum(), (cm15 & (g.depth_m<15)).sum(), 100*(cm15&(g.depth_m<15)).sum()/max(cm15.sum(),1)))
# regional breakdown
regs = {'Jurong/PasirPanjang approach':(1.15,1.26,103.62,103.80),'Tuas':(1.17,1.30,103.55,103.68),'Main strait central':(1.12,1.22,103.80,104.05),'East gateway':(1.20,1.35,104.10,104.45)}
for r,(a,b,c,d) in regs.items():
    m = sea & (LAT>=a)&(LAT<=b)&(LON>=c)&(LON<=d)
    if m.sum()<20: print("  %s: n=%d"%(r,m.sum())); continue
    e = g.depth_m[m]-mid[m]
    print("  %-30s n=%5d MAE=%.1f bias=%+.1f  deeper-than-DRVAL2: %.0f%%" % (r, m.sum(), np.abs(e).mean(), e.mean(), 100*(g.depth_m[m]>dr2[m]+0.5).mean()))


# ---------- Coastline ----------
cA = load('coastlinea','CoastlineA')
print("\nCOASTLINE A: features", len(cA), "SORDAT:", collections.Counter(p['SORDAT'] for _,p in cA).most_common(3))
print(" CATSLC:", collections.Counter(p.get('CATSLC_TXT') for _,p in cA).most_common(6))
print(" WATLEV:", collections.Counter(p.get('WATLEV_TXT') for _,p in cA).most_common(6))

print("\n=========== layers, lanes, coastline, hazards, berths ===========")
# chart cells present (scale band = 5th char of DSNM, S-57 usage band)
for n,nm in (('depthsa','DepthsA'),('coastlinea','CoastlineA'),('tracksandroutesa','TracksAndRoutesA'),('dangersp','DangersP'),('portsandservicesp','PortsAndServicesP'),('regulatedareasandlimitsa','RegulatedAreasAndLimitsA')):
    d=load(n,nm); c=collections.Counter(p['DSNM'][:3]+'..'+p['DSNM'][3:7] for _,p in d)
    print(nm,'chart cells:',dict(sorted(c.items())))
# union extent of DepthsA
from shapely.ops import unary_union
dA=[s for s,_ in load('depthsa','DepthsA')]; u=unary_union(dA); print('DepthsA union bounds',[round(x,3) for x in u.bounds],'area share of bbox %.1f%%'%(100*u.intersection(bb).area/bb.area))
# TSS
tA=load('tracksandroutesa','TracksAndRoutesA')
print('\nTRACKS A: CATTSS',collections.Counter(p.get('CATTSS_TXT') for _,p in tA)); print(' TRAFIC',collections.Counter(p.get('TRAFIC_TXT') for _,p in tA))
rows=[(p.get('OBJNAM'),p.get('CATTSS_TXT'),p.get('TRAFIC_TXT'),p.get('ORIENT'),round(s.centroid.y,3),round(s.centroid.x,3),round(s.area*111*111,1)) for s,p in tA if p.get('CATTSS_TXT')]
for r in sorted(rows,key=lambda r:(r[5])): print('  ',r)
tL=load('tracksandroutesl','TracksAndRoutesL'); print(' L CATTRK',collections.Counter(p.get('CATTRK_TXT') for _,p in tL),' CATNAV',collections.Counter(p.get('CATNAV_TXT') for _,p in tL))
# land: rasterise CoastlineA (+ gaps) vs model land
cA=[s for s,p in load('coastlinea','CoastlineA') if p.get('WATLEV_TXT')=='always dry' or p.get('WATLEV_TXT') is None]
print('\nCoastlineA bounds',[round(x,3) for x in unary_union(cA).bounds])
# model land total km2 and Tuas area comparison
cell=(g.resolution_deg*111.0)**2*np.cos(np.radians(1.3)); print('model land km2 %.0f'%(g.land.sum()*cell))
pierlike=[s for s,p in load('coastlinea','CoastlineA') if p.get('CATSLC_TXT') in('pier (jetty)','breakwater')]
cm=np.zeros(g.land.shape,bool)
for s in pierlike:
    x0,y0,x1,y1=s.bounds; ix0=max(int((x0-g.lons[0])/g.resolution_deg)-1,0); ix1=min(int((x1-g.lons[0])/g.resolution_deg)+2,g.nx); iy0=max(int((y0-g.lats[0])/g.resolution_deg)-1,0); iy1=min(int((y1-g.lats[0])/g.resolution_deg)+2,g.ny)
    if ix1>ix0 and iy1>iy0: cm[iy0:iy1,ix0:ix1]|=contains_xy(s,LON[iy0:iy1,ix0:ix1],LAT[iy0:iy1,ix0:ix1])
print('pier/jetty/breakwater cells %d; model marks %.0f%% of them as land'%(cm.sum(),100*(cm&g.land).sum()/max(cm.sum(),1)))
# dangers inside strait bbox and on cells model thinks navigable
dP=load('dangersp','DangersP'); inside=[(s,p) for s,p in dP if bb.contains(s.centroid)]
print('\nDANGERS P in bbox',len(inside),'of',len(dP)); print(' WATLEV',collections.Counter(p.get('WATLEV_TXT') for _,p in inside)); print(' CATOBS',collections.Counter(p.get('CATOBS_TXT') for _,p in inside).most_common(5),' CATWRK',collections.Counter(p.get('CATWRK_TXT') for _,p in inside).most_common(4))
vs=[p['VALSOU'] for _,p in inside if p.get('VALSOU') is not None]; print(' VALSOU m: n=%d, <10 m: %d, <15 m: %d'%(len(vs),sum(v<10 for v in vs),sum(v<15 for v in vs)))
nav=0; lowdepth=0
for s,p in inside:
    iy,ix=g.to_index(s.centroid.y,s.centroid.x)
    if p.get('VALSOU') is not None and p['VALSOU']<15 and not g.land[iy,ix] and g.depth_m[iy,ix]>=15: lowdepth+=1
print(' hazards shallower than 15 m sitting in model cells >=15 m deep:',lowdepth)
# berths
pP=load('portsandservicesp','PortsAndServicesP'); b=[(p['OBJNAM'],round(s.centroid.y,4),round(s.centroid.x,4),p.get('DRVAL1')) for s,p in pP if p.get('OBJNAM') and 'Berth' in p['OBJNAM']]
print('\nBERTH points named:',len(b)); print(' terminals:',collections.Counter(o.split(' Berth')[0] for o,*_ in b).most_common(12))

