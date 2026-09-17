"""Run the delivered algorithm directly on one complete JSON recording."""
from pathlib import Path
import argparse,json
from detector import load_model,scan_json
PACKAGE=Path(__file__).resolve().parent
def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input',required=True);ap.add_argument('--cow-id',required=True);ap.add_argument('--output',required=True)
    ap.add_argument('--threads',type=int,default=2)
    a=ap.parse_args();src=Path(a.input).resolve();dst=Path(a.output).resolve();files=[dst,dst.with_suffix('.run.json'),dst.with_suffix('.quality.csv')]
    if src in files or any(p.exists() for p in files):raise ValueError('Choose unused output paths; no original file or previous output is overwritten')
    pack=load_model(PACKAGE/'models/model.pkl');prompts,info,q=scan_json(src,a.cow_id,pack,threads=a.threads)
    dst.parent.mkdir(parents=True,exist_ok=True);prompts.to_csv(dst,index=False,encoding='utf-8-sig');q.to_csv(files[2],index=False,encoding='utf-8-sig')
    info['profile']='strict91';info['accuracy']=None;files[1].write_text(json.dumps(info,indent=2),encoding='utf-8');print(json.dumps(info))
if __name__=='__main__':main()
