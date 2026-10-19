from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
DB_PATH=ROOT/"data/heatplan.duckdb"
FEATURES={
"canopy_cover":("树冠覆盖率","比例"),
"canopy_height":("冠层平均高度","m"),
"canopy_variance":("冠层高度方差","m²"),
"canopy_volume":("冠层体积","m³"),
"building_cover":("建筑覆盖率","比例"),
"building_height":("建筑平均高度","m"),
"building_variance":("建筑高度方差","m²"),
"building_volume":("建筑体积","m³"),
}
DISTRICTS={"Daxing":"大兴区","Shijingshan":"石景山区","Haidian":"海淀区","Chaoyang":"朝阳区","Fengtai":"丰台区","Dongcheng":"东城区","Xicheng":"西城区"}
SCENARIO="北京五环内 · 极端高温周模拟平均 UTCI · 10 m 缓冲区"
EXCLUDED_IDS=[281312,281313,281314,281315,389841,389842,389843,389844,434771,512213]
