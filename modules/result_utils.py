import pandas as pd 
import json 
import os
PROVINCES = {
    "Vinh Long" : "Vĩnh Long",
    "An Giang" : "An Giang",
    "Ca Mau" : "Cà Mau",
    "Can Tho" : "TP. Cần Thơ",
    "Dong Nai" : "Đồng Nai",
    "Dong Thap" : "Đồng Tháp",
    "Tay Ninh" : "Tây Ninh",
    "Cao Bang" : "Cao Bằng",
    "Hai Phong" : "TP. Hải Phòng",
    "Lao Cai" : "Lào Cai",
    "Thai Nguyen" : "Thái Nguyên",
    "Tuyen Quang" : "Tuyên Quang",
    "Bac Ninh" : "Bắc Ninh",
    "Hung Yen" : "Hưng Yên",
    "Ninh Binh" : "Ninh Bình",
    "Ha Tinh" : "Hà Tĩnh",
    "Hue" : "TP. Huế",
    "Nghe An" : "Nghệ An",
    "Quang Ninh" : "Quảng Ninh",
    "Quang Tri" : "Quảng Trị",
    "Dien Bien" : "Điện Biên",
    "Ha Noi" : "TP. Hà Nội",
    "Lai Chau" : "Lai Châu",
    "Lang Son" : "Lạng Sơn",
    "Son La" : "Sơn La",
    "Da Nang" : "TP. Đà Nẵng",
    "Dak Lak" : "Đắk Lắk",
    "Gia Lai" : "Gia Lai",
    "Khanh Hoa" : "Khánh Hòa",
    "Quang Ngai" : "Quảng Ngãi",
    "Thanh Hoa" : "Thanh Hóa",
    "Lam Dong" : "Lâm Đồng",
    "Ho Chi Minh" : "TP. Hồ Chí Minh",
    "Phu Tho" : "Phú Thọ"
}

def _init_valid_provinces():
    possible_paths = [
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "DS_KN_TRA_CUU_1784690544201 - DanhSach.csv")),
        "DS_KN_TRA_CUU_1784690544201 - DanhSach.csv",
    ]
    csv_path = None
    for p in possible_paths:
        if os.path.exists(p):
            csv_path = p
            break
    if not csv_path:
        return []
    df = pd.read_csv(csv_path)
    list_valid_authories = df["Địa phương"].to_list()
    list_valid_authories = list(set(list_valid_authories))
    return list_valid_authories

def _check(list_provinces, valid_provinces):
    if not valid_provinces:
        return
    for province in list_provinces:
        if province not in valid_provinces:
            raise ValueError(f"[ERROR] Province {province} not in valid provinces from dataframe")
    

VALID_PROVINCES = _init_valid_provinces()
_check(list(PROVINCES.values()), VALID_PROVINCES)



class ResultExporter(object):
    def __init__(self):
        self.KEYS = ["STT", "Nội dung kiến nghị", "Nội dung trả lời", "Địa phương", "Thẩm quyền giải quyết", "Địa phương bỏ dấu"]
        self.data = {}
        for key in self.KEYS:
            self.data[key] = []
        self.count = 1
    
    def _match_province(self, pdf_path: str):
        filename = pdf_path.split("/")[-1]
        for province in PROVINCES:
            if province in filename:
                return PROVINCES[province]
        raise ValueError(f"[ERROR] Filename: {pdf_path} does not contain any province name")
        
    def add(self, list_results: list, pdf_path: str):
        for result in list_results:
            self.data[self.KEYS[0]].append(self.count)
            self.data[self.KEYS[1]].append(result["content"])
            self.data[self.KEYS[2]].append("")
            self.data[self.KEYS[3]].append(self._match_province(pdf_path))
            self.data[self.KEYS[4]].append(result["authority"])
            self.data[self.KEYS[5]].append("")
            self.count += 1

    def export(self, path: str):
        pd.DataFrame(self.data).to_csv(path, index=False, header=True, encoding="utf-8")

class MultiAuthorityResultExporter(ResultExporter):
    def __init__(self):
        super().__init__()
        
    def add(self, list_kien_nghi: list, pdf_path: str, confidence_threshold: list[str] = ["high", "medium", "low"]):
        for result in list_kien_nghi:
            self.data[self.KEYS[0]].append(self.count)
            self.data[self.KEYS[1]].append(result["noi_dung"])
            self.data[self.KEYS[2]].append("")
            self.data[self.KEYS[3]].append(self._match_province(pdf_path))
            raw_authorities = [element["ten_don_vi"] for element in result["don_vi_nhan"] if element["muc_do_tin_cay"] in confidence_threshold]
            authority_string = raw_authorities[0] if len(raw_authorities) > 0 else ""
            if len(raw_authorities) > 1:
                for i in range(1, len(raw_authorities)):
                    authority_string += ";" + raw_authorities[i]
            self.data[self.KEYS[4]].append(authority_string)
            self.data[self.KEYS[5]].append("")
            self.count += 1

    def export(self, path: str):
        pd.DataFrame(self.data).to_csv(path, index=False, header=True, encoding="utf-8")