"""Shortcut settings and key matching; no typed text is retained."""
from copy import deepcopy

ACTIONS={
    'earlier':(1,'가사 0.25초 앞당기기'), 'later':(2,'가사 0.25초 늦추기'),
    'previous':(3,'이전 구절'), 'next':(4,'다음 구절'), 'stamp':(5,'현재 구절 끝 / 다음으로 전환 기록'),
    'lock':(6,'클릭 통과 켜기 / 끄기'), 'chrome':(7,'헤더 숨기기 / 복원'),
    'transparent':(8,'배경 투명 켜기 / 끄기'), 'editor':(9,'곡 / 싱크 편집'),
    'follow':(10,'자동 따라가기'), 'align':(11,'현재 구절을 지금에 맞추기')}
DEFAULTS={action:{'key':key,'modifiers':3,'shift':'any'} for action,key in zip(ACTIONS,[0x25,0x27,0x26,0x28,0x0D,0x4C,0x48,0x54,0x45,0x41,0x20])}
DEFAULTS['stamp']={'key':0x0D,'modifiers':4,'shift':'right'}
KEY_NAMES={0x0D:'Enter',0x20:'Space',0x09:'Tab',0x1B:'Esc',0x08:'Backspace',0x2E:'Delete',0x2D:'Insert',0x24:'Home',0x23:'End',0x21:'PageUp',0x22:'PageDown',0x25:'←',0x26:'↑',0x27:'→',0x28:'↓'}
KEY_NAMES.update({key:chr(key) for key in range(0x30,0x5B) if key<=0x39 or key>=0x41})
KEY_NAMES.update({0x70+i:f'F{i+1}' for i in range(24)})
KEY_NAMES.update({0x60+i:f'Num{i}' for i in range(10)})
KEY_NAMES.update({0x6A:'Num*',0x6B:'Num+',0x6D:'Num-',0x6E:'Num.',0x6F:'Num/',0xBA:';',0xBB:'=',0xBC:',',0xBD:'-',0xBE:'.',0xBF:'/',0xC0:'`',0xDB:'[',0xDC:'\\',0xDD:']',0xDE:"'"})

def validate_bindings(overrides=None):
    result=deepcopy(DEFAULTS)
    if overrides is not None:
        if not isinstance(overrides,dict):raise ValueError('단축키 설정 형식이 올바르지 않습니다.')
        result.update({key:value for key,value in overrides.items() if key in ACTIONS})
    seen=[]
    for action,value in result.items():
        if value is None:continue
        if not isinstance(value,dict):raise ValueError('단축키 설정 형식이 올바르지 않습니다.')
        key=value.get('key');mods=value.get('modifiers');side=value.get('shift','any')
        if type(key)!=int or key not in KEY_NAMES or type(mods)!=int or mods<0 or mods>15 or side not in ('any','left','right'):
            raise ValueError('지원하지 않는 키 조합입니다.')
        if not mods and not (0x70<=key<=0x87 or 0x25<=key<=0x28):raise ValueError('문자·숫자 키에는 Ctrl, Alt 또는 Shift를 함께 눌러 주세요. 방향키와 F1–F24는 단독 지정할 수 있습니다.')
        if side!='any' and not mods&4:raise ValueError('왼쪽/오른쪽 Shift를 다시 지정해 주세요.')
        for other,binding in seen:
            if key==binding['key'] and mods==binding['modifiers'] and (side==binding['shift'] or 'any' in (side,binding['shift'])):
                raise ValueError(ACTIONS[other][1]+'와 같은 단축키입니다.')
        result[action]={'key':key,'modifiers':mods,'shift':side};seen.append((action,result[action]))
    return result

def shortcut_label(binding):
    if binding is None:return '사용 안 함'
    parts=[];mods=binding['modifiers']
    if mods&2:parts.append('Ctrl')
    if mods&1:parts.append('Alt')
    if mods&4:parts.append({'right':'오른쪽 Shift','left':'왼쪽 Shift','any':'Shift'}[binding.get('shift','any')])
    if mods&8:parts.append('Win')
    return '+'.join([*parts,KEY_NAMES[binding['key']]])

class ShortcutMatcher:
    modifier_bits={0xA0:4,0xA1:4,0xA2:2,0xA3:2,0xA4:1,0xA5:1,0x5B:8,0x5C:8}
    def __init__(self,bindings,held=()):
        self.bindings=bindings;self.held=set(held)&self.modifier_bits.keys()
        self.keys={value['key'] for value in bindings.values() if value};self.down=set(held)&self.keys;self.captured=set()
    def event(self,key,down,enabled=True,blocked=False):
        if not enabled:self.captured.clear()
        if key in self.modifier_bits:
            if down:self.held.add(key)
            else:self.held.discard(key)
            return False,None
        if key not in self.keys:return False,None
        if not enabled or blocked:
            if down:self.down.add(key)
            else:self.down.discard(key)
            return False,None
        if not down:
            consumed=key in self.captured;self.down.discard(key);self.captured.discard(key)
            return consumed,None
        if key in self.down:return key in self.captured,None
        self.down.add(key);mods=0
        for held in self.held:mods|=self.modifier_bits[held]
        for action,binding in self.bindings.items():
            if not binding or binding['key']!=key or binding['modifiers']!=mods:continue
            side=binding['shift']
            if side=='right' and 0xA1 not in self.held:continue
            if side=='left' and 0xA0 not in self.held:continue
            self.captured.add(key);return True,action
        return False,None
