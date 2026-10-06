#!/usr/bin/env python3
"""Configure a portable Agent Skill without overwriting app settings."""
import argparse,json,os,platform,shlex,shutil,sys
from pathlib import Path

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--app',required=True,choices=['codex','claude','cursor','antigravity','other'])
    p.add_argument('--project',type=Path,help='Project receiving the skill; use --skill-dir for a custom/global skills directory')
    p.add_argument('--skill-dir',type=Path)
    p.add_argument('--apply',action='store_true',help='Create the skill symlink; app MCP settings are never edited')
    a=p.parse_args()
    if platform.system() not in ('Darwin','Linux'):
        p.error('This release needs macOS or Linux; native Windows process control is not supported')
    if sys.version_info < (3,10):p.error('Python 3.10+ required')
    root=Path(__file__).resolve().parents[1]
    skill=root/'skills/delegate-to-agy'
    if not (root/'dashboard/dist/index.html').is_file():p.error('Dashboard build missing; clone the complete repository')
    folders={'codex':'.agents','claude':'.claude','cursor':'.cursor','antigravity':'.agents'}
    if a.skill_dir: directory=a.skill_dir.expanduser().resolve()
    elif a.project and a.app!='other':directory=a.project.expanduser().resolve()/folders[a.app]/'skills'
    else:p.error('Choose --project or --skill-dir; app settings are not guessed')
    dest=directory/'delegate-to-agy'
    if dest.is_symlink() and dest.resolve()==skill:pass
    elif dest.exists() or dest.is_symlink():p.error('Existing skill preserved: '+str(dest))
    elif a.apply:
        directory.mkdir(parents=True,exist_ok=True);dest.symlink_to(skill,target_is_directory=True)
    entry=str(skill/'scripts/agy_mcp.py')
    config={'mcpServers':{'agy-delegator':{'command':sys.executable,'args':[entry],'env':{'PATH':os.environ.get('PATH','')}}}}
    print(('Installed skill: ' if a.apply else 'Preview skill location: ')+str(dest))
    print('Keep the extracted package at: '+str(root))
    print('MCP configuration (merge this entry into your app; preserve other servers):')
    print(json.dumps(config,indent=2))
    if a.app in ('codex','claude'):
        args=([a.app,'mcp','add','--env','PATH='+os.environ.get('PATH',''),'agy-delegator','--'] if a.app=='codex' else ['claude','mcp','add','--transport','stdio','--scope','user','--env','PATH='+os.environ.get('PATH',''),'agy-delegator','--'])+[sys.executable,entry]
        print('Register MCP separately:\n'+shlex.join(args))
    print('AGY CLI: '+(shutil.which('agy') or 'not found; install it and sign in first'))
    print('No credentials, AGY sessions, or application settings were modified.')
if __name__=='__main__':main()
