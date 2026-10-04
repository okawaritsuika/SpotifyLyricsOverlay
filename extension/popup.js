function readerRequest(form) {
  const url=new URL(form.url);
  const allowed=typeof module!=='undefined'?require('./document_match.js').documentURL:documentURL;
  if(!allowed(url.href))throw new Error('나무위키 또는 SideM KO 위키의 문서 주소를 입력하세요.');
  const size=Number(form.size);
  if(!Number.isFinite(size)||size<12||size>48)throw new Error('글자 크기는 12–48입니다.');
  return {action:'open-reader',url:url.href,options:{selector:form.selector.trim(),size},smallWindow:!!form.smallWindow};
}
async function openReader(api,form) {
  const request=readerRequest(form);
  const granted=await api.permissions.request({origins:[new URL(request.url).origin+'/*']});
  if(!granted)throw new Error('이 사이트의 표시를 바꾸려면 사이트 접근 허용이 필요합니다.');
  const result=await api.runtime.sendMessage(request);
  if(!result?.ok)throw new Error(result?.error||'페이지를 열지 못했습니다.');
  return result;
}
if(typeof module!=='undefined')module.exports={readerRequest,openReader};
if(typeof document!=='undefined') {
  document.getElementById('version').textContent='v'+chrome.runtime.getManifest().version;
  let automatic=false;
  const autoButton=document.getElementById('auto');
  const autoStatus=document.getElementById('auto-status');
  const retryButton=document.getElementById('retry');
  const choices=document.getElementById('candidates');
  const showChoices=value=>{
    choices.replaceChildren();
    for(const url of value?.urls||[]){
      const button=document.createElement('button');button.className='secondary';button.type='button';
      button.textContent='이 문서 연결: '+documentName(url);
      button.style.overflowWrap='anywhere';
      button.addEventListener('click',()=>run(async()=>{
        const reply=await chrome.runtime.sendMessage({action:'choose-candidate',url});
        if(!reply?.ok)throw new Error(reply?.error||'후보 연결 실패');return '선택한 문서를 연결합니다.';
      }));choices.append(button);
    }
  };
  const showAutomatic=value=>{automatic=!!value;retryButton.disabled=!automatic;autoButton.textContent=automatic?'Spotify 자동 연동 끄기':'Spotify 자동 연동 켜기';};
  chrome.storage.local.get({spotifyEnabled:false,spotifySize:22,spotifySelector:''}).then(value=>{
    showAutomatic(value.spotifyEnabled);document.getElementById('size').value=value.spotifySize;document.getElementById('selector').value=value.spotifySelector;
  });
  chrome.storage.session.get({spotifyStatus:'자동 연동 꺼짐',spotifyChoices:null}).then(value=>{autoStatus.textContent=value.spotifyStatus;showChoices(value.spotifyChoices);});
  chrome.storage.onChanged.addListener((changes,area)=>{
    if(area==='session'&&changes.spotifyStatus)autoStatus.textContent=changes.spotifyStatus.newValue;
    if(area==='session'&&changes.spotifyChoices)showChoices(changes.spotifyChoices.newValue);
    if(area==='local'&&changes.spotifyEnabled)showAutomatic(changes.spotifyEnabled.newValue);
  });
  const status=document.getElementById('status');
  const options=()=>({selector:document.getElementById('selector').value.trim(),size:Number(document.getElementById('size').value)});
  const run=async task=>{
    try{status.textContent='처리 중…';const message=await task();status.textContent=message||'적용했습니다. 영역 선택 중이면 원본 페이지에서 원하는 부분을 클릭하세요.';}
    catch(error){status.textContent=error.message;}
  };
  autoButton.addEventListener('click',()=>run(async()=>{
    const enabled=!automatic;
    if(enabled && !await chrome.permissions.request({origins:['https://namu.wiki/*','https://sidem-ko.fandom.com/*','http://127.0.0.1/*']}))
      throw new Error('Spotify 자동 연동의 사이트·로컬 앱 연결 권한이 필요합니다.');
    const result=await chrome.runtime.sendMessage({action:'set-automatic',enabled,options:options()});
    if(!result?.ok)throw new Error(result?.error||'자동 연동을 설정하지 못했습니다.');
    showAutomatic(enabled);
    return enabled?'자동 연동을 켰습니다. Spotify 재생 중에는 위 상태에서 가사창 연결 결과를 확인하세요.':'자동 연동을 껐습니다.';
  }));
  retryButton.addEventListener('click',()=>run(async()=>{
    if(!automatic)throw new Error('자동 연동을 먼저 켜세요.');
    const result=await chrome.runtime.sendMessage({action:'set-automatic',enabled:true,options:options()});
    if(!result?.ok)throw new Error(result?.error||'다시 연결하지 못했습니다.');
    return '현재 곡 연결을 다시 시작했습니다. 위 상태를 확인하세요.';
  }));
  document.getElementById('open').addEventListener('click',()=>run(()=>openReader(chrome,{
    url:document.getElementById('url').value,...options(),smallWindow:document.getElementById('small').checked
  })));
  for(const [id,action] of [['current','apply-reader'],['restore','restore-reader']]) {
    document.getElementById(id).addEventListener('click',()=>run(async()=>{
      const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
      const result=await chrome.runtime.sendMessage({action,tabId:tab?.id,options:options()});
      if(!result?.ok)throw new Error(result?.error||'이 페이지에는 적용할 수 없습니다.');
      if(action==='apply-reader')window.close();
    }));
  }
  chrome.tabs.query({active:true,currentWindow:true}).then(([tab])=>{
    if(/^https?:\/\//.test(tab?.url||''))document.getElementById('url').value=tab.url;
  });
}

