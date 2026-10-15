// The Baseten chat-agent trial is complete. This page reads its archive only.
(() => {
  const feed = document.getElementById('feed');
  const refreshButton = document.getElementById('refresh-log');
  const names = {filler: 'Filler', reviewer: 'Reviewer', coordinator: '调度者'};
  let busy = false;

  async function refresh() {
    if (busy) return;
    busy = true;
    refreshButton.disabled = true;
    try {
      const response = await fetch('/api/agent-lab/live-team', {cache: 'no-store'});
      if (!response.ok) throw new Error('Archive unavailable');
      const payload = await response.json();
      if (!Array.isArray(payload.events)) throw new Error('Invalid archive');
      feed.replaceChildren();
      const events = payload.events.filter(event => event && typeof event === 'object');
      for (const event of events) {
        const role = Object.hasOwn(names, event.role) ? event.role : 'coordinator';
        const row = document.createElement('div');
        row.className = 'message ' + role;
        const avatar = document.createElement('div');
        avatar.className = 'avatar';
        avatar.textContent = {filler: 'F', reviewer: 'R', coordinator: '↳'}[role];
        const content = document.createElement('div');
        const line = document.createElement('div');
        line.className = 'byline';
        const name = document.createElement('b');
        name.textContent = names[role] + ' → ' + (Object.hasOwn(names, event.to) ? names[event.to] : '协作组');
        const time = document.createElement('small');
        const date = new Date(Number(event.at) * 1000);
        time.textContent = Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN') : '';
        const body = document.createElement('div');
        body.className = 'body';
        body.textContent = String(event.text || '');
        line.append(name, time);
        content.append(line, body);
        row.append(avatar, content);
        feed.append(row);
      }
      if (!events.length) {
        const empty = document.createElement('div');
        empty.className = 'empty';
        empty.textContent = '当前未读到历史消息。此测试已完成，可稍后刷新记录。';
        feed.append(empty);
      }
      document.getElementById('count').textContent = events.length;
      document.getElementById('last-update').textContent = '历史记录载入于 ' + new Date().toLocaleTimeString('zh-CN');
    } catch (_) {
      document.getElementById('last-update').textContent = '历史记录暂未载入。点击“刷新历史记录”重试。';
    } finally {
      busy = false;
      refreshButton.disabled = false;
    }
  }
  refreshButton.addEventListener('click', refresh);
  refresh();
})();
