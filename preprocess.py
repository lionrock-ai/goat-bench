from pathlib import Path
import gzip
import json


def preprocess(path: Path):
    files = [i for i in path.iterdir() if i.name.endswith('.gz')]
    for file in files:
        with gzip.open(file, 'rb') as f:
            data = json.load(f)
        goals = {}
        for i in data['goals'].values():
            for j in i:
                goals[j['object_id']] = j
        out = {}
        out['scene_id'] = data['episodes'][0]['scene_id']
        out['episodes'] = []
        for i in data['episodes']:
            episode = {}
            for k in ['start_position', 'start_rotation', 'episode_id']:
                episode[k] = i[k]
            episode['tasks'] = []
            for j in i['tasks']:
                task = {}
                task['category'] = j[0]
                object_id = j[2]
                if object_id is not None:
                    task['description'] = goals[object_id].get('lang_desc')
                    task['position'] = goals[object_id]['position']
                else:
                    task['description'] = None
                    task['position'] = None
                episode['tasks'].append(task)
            out['episodes'].append(episode)
        out_file = file.with_suffix('')
        with open(out_file, 'w') as f:
            json.dump(out, f, indent=4)
                
for split in ['train', 'val_unseen', 'val_seen', 'val_seen_synonyms']:
    preprocess(Path(f'data/datasets/goat_bench/hm3d/v1/{split}/content'))
