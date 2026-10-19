"""Source-road geometry partitioned at midpoints of measured point stations.

No edges are invented between unrelated roads. Each multipart component is
handled separately; components without a matched model point stay uncoloured.
"""
import json
import numpy as np
import shapely
from pyproj import Transformer
from heatplan.decision import type_sql


def midpoint_cells(coordinates, stations):
    """Yield (owner index, start station, end station, polyline) in metres.

    Tie stations share no unique interval: the first stable point ID owns it.
    The caller records the number of these ties instead of inventing geometry.
    """
    xy=np.asarray(coordinates,dtype=float)[:,:2]
    lengths=np.hypot(*np.diff(xy,axis=0).T)
    xy=xy[np.r_[True,lengths>1e-9]]
    if len(xy)<2:return
    distance=np.r_[0.,np.cumsum(np.hypot(*np.diff(xy,axis=0).T))]
    order=np.argsort(stations,kind="stable")
    values=np.clip(np.asarray(stations)[order],0.,distance[-1])
    keep=np.r_[True,np.diff(values)>1e-7]
    order,values=order[keep],values[keep]
    boundaries=np.r_[0.,(values[:-1]+values[1:])/2,distance[-1]]
    endpoints=np.column_stack([np.interp(boundaries,distance,xy[:,i]) for i in (0,1)])
    for j,owner in enumerate(order):
        lo,hi=boundaries[j:j+2]
        a=np.searchsorted(distance,lo,side="right")
        b=np.searchsorted(distance,hi,side="left")
        path=np.vstack([endpoints[j],xy[a:b],endpoints[j+1]])
        yield int(owner),float(lo),float(hi),path


def build_network(analysis,district):
    forward=Transformer.from_crs(4326,32650,always_xy=True)
    reverse=Transformer.from_crs(32650,4326,always_xy=True)
    # Use all samples on a selected source road before district filtering, so
    # the last in-district point does not colour its out-of-district remainder.
    params=[]
    road_filter=""
    if district!="全部区域":
        road_filter=" WHERE street_id IN (SELECT DISTINCT street_id FROM points WHERE district=? AND data_usable)"
        params=[district]
    roads=analysis.query("SELECT street_id,street_name,osm_id,geometry_json FROM roads"+road_filter,params)
    points=analysis.query("SELECT street_id,point_id,x,y,utci,district,"+type_sql()+" AS problem_type FROM points WHERE data_usable"+
        (" AND street_id IN (SELECT DISTINCT street_id FROM points WHERE district=? AND data_usable)" if params else "")+" ORDER BY point_id",params)
    groups={int(sid):g for sid,g in points.groupby("street_id",sort=False)}
    segments=[]
    audit=dict(district=district,source_roads=len(roads),matched_samples=len(points),displayed_samples=0,
        duplicate_stations=0,unmeasured_components=0,source_length_m=0.,partition_length_m=0.,max_point_to_source_m=0.)
    for row in roads.itertuples(index=False):
        sid=int(row.street_id);g=groups.get(sid)
        geom=json.loads(row.geometry_json)
        parts=geom["coordinates"] if geom["type"]=="MultiLineString" else [geom["coordinates"]]
        parts=[np.column_stack(forward.transform(*np.asarray(p)[:,:2].T)) for p in parts if len(p)>=2]
        lines=[shapely.LineString(p) for p in parts]
        if not lines:continue
        samples=[] if g is None else g.to_dict("records")
        if samples:
            pts=shapely.points(g.x.to_numpy(),g.y.to_numpy())
            distances=np.column_stack([shapely.distance(line,pts) for line in lines])
            component=np.argmin(distances,axis=1)
            audit["max_point_to_source_m"]=max(audit["max_point_to_source_m"],float(distances.min(axis=1).max()))
        else:component=np.array([],dtype=int)
        for index,(line,xy) in enumerate(zip(lines,parts)):
            audit["source_length_m"]+=float(line.length)
            indices=np.flatnonzero(component==index)
            if not len(indices):
                audit["unmeasured_components"]+=1
                audit["partition_length_m"]+=float(line.length)
                lon,lat=reverse.transform(*xy.T)
                segments.append(dict(path=np.round(np.column_stack([lon,lat]),7).tolist(),street_id=sid,
                    street_name=row.street_name or "",osm_id=str(row.osm_id or ""),
                    point_id=None,utci=None,problem_type="unmeasured"))
                continue
            positions=shapely.line_locate_point(line,pts[indices])
            cells=list(midpoint_cells(xy,positions))
            audit["duplicate_stations"]+=len(indices)-len(cells)
            for owner,start,end,path in cells:
                audit["partition_length_m"]+=end-start
                sample=samples[int(indices[owner])]
                if district!="全部区域" and sample["district"]!=district:continue
                lon,lat=reverse.transform(*path.T)
                segments.append(dict(path=np.round(np.column_stack([lon,lat]),7).tolist(),street_id=sid,
                    street_name=row.street_name or "",osm_id=str(row.osm_id or ""),
                    point_id=int(sample["point_id"]),utci=float(sample["utci"]),problem_type=sample["problem_type"]))
                audit["displayed_samples"]+=1
    audit["displayed_cells"]=len(segments)
    audit["length_error_m"]=abs(audit["source_length_m"]-audit["partition_length_m"])
    return segments,audit
