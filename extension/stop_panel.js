// When an application stops, the page itself says what it could not fill and takes the owner's reply.
// The shadow root is closed and only real clicks count, so the employer's page cannot read or press this.
(() => {
  if (globalThis.JobScanStopPanel) return;
  // On the left: the main panel holds the top right and paints above this one, which would hide what it says.
  const STYLE = `
    :host{all:initial}
    .box{position:fixed;top:16px;left:16px;width:380px;max-height:calc(100vh - 32px);overflow:auto;z-index:2147483647;box-sizing:border-box;
      background:#fff;color:#1c3933;border:2px solid #b3372f;border-radius:10px;box-shadow:0 8px 28px rgba(0,0,0,.25);padding:14px 16px;
      font:14px/1.45 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}
    h2{margin:0 0 4px;font-size:15px;color:#b3372f}
    p{margin:0 0 8px;overflow-wrap:anywhere}
    .why{color:#73857e;font-size:12px}
    .item{border-top:1px solid #dce5df;padding:10px 0}
    .label{font-weight:600;cursor:pointer}
    .label:hover{text-decoration:underline}
    .reason{color:#a37026;font-size:12px}
    .done{color:#216b51}
    .row{display:flex;gap:6px;flex-wrap:wrap;align-items:flex-end}
    textarea{flex:1 1 200px;font:inherit;padding:6px 8px;border:1px solid #dce5df;border-radius:6px;min-width:0;box-sizing:border-box}
    button{font:inherit;padding:5px 10px;border:1px solid #216b51;border-radius:6px;background:#fff;color:#216b51;cursor:pointer}
    button.primary{background:#216b51;color:#fff}
    .foot{border-top:1px solid #dce5df;padding-top:10px;display:flex;gap:6px;flex-wrap:wrap}
    .close{position:absolute;top:8px;right:10px;border:0;color:#73857e;padding:2px 6px}`;

  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }
  // Only a person's click: a script on the employer's page produces untrusted events.
  function press(node, action) {
    node.onclick = event => { if (event.isTrusted) action(); };
    return node;
  }

  // What stopped this form, one row per question. A field with two complaints is one question, and a title the form repeats
  // ("Education History", four times) gets no answer box: one answer would be kept for every later form with that title.
  function rowsFrom({issues, fields, jobId}) {
    const key = field => `${field.label}\n${field.description || ''}`;
    const copies = new Map();
    for (const field of fields) copies.set(key(field), (copies.get(key(field)) || 0) + 1);
    const items = [], pageIssues = [], byField = new Map(), byTitle = new Map();
    for (const issue of issues) {
      const field = fields.find(f => f.id === issue.field_id);
      if (!field) { pageIssues.push(issue.reason); continue; }
      const known = byField.get(field.id);
      if (known) {
        if (!known.reason.split('；').includes(issue.reason)) known.reason += '；' + issue.reason;
        continue;
      }
      const repeated = copies.get(key(field)) > 1;
      const twin = repeated && byTitle.get(key(field));
      if (twin) { twin.also.push(field.id); byField.set(field.id, twin); continue; }
      const item = {field_id: field.id, also: [], job_id: jobId, label: field.label, description: field.description || '', reason: issue.reason, value: field.value,
                    options: field.options || [], answerable: field.kind !== 'file' && !repeated, repeated};
      items.push(item); byField.set(field.id, item);
      if (repeated) byTitle.set(key(field), item);
    }
    return {items, pageIssues};
  }

  // `clicked`: Submit was already pressed on this page, so the application may be with the employer.
  // `verification`: that click brought a code step. The employer is waiting for the code, and pressing Submit again is the way on.
  function show({company, reason, items, pageIssues, elementsFor, send, clicked = false, verification = false, mailcode = true}) {
    document.getElementById('jobscan-stop-panel')?.remove();
    const host = make('div');
    host.id = 'jobscan-stop-panel';
    const root = host.attachShadow({mode: 'closed'});
    const style = make('style');
    style.textContent = STYLE;
    const box = make('div', 'box');
    root.append(style, box);
    const heading = make('h2', '', verification ? '还差验证码，这份还没投完' : clicked ? '可能已投出，别再提交' : '这份停下了，还没有提交');
    box.append(press(make('button', 'close', '×'), () => host.remove()), heading, make('p', 'why', company));
    if (reason && (clicked || (!items.length && !pageIssues.length))) box.append(make('p', 'reason', reason));

    const outline = item => {
      for (const el of [item.field_id, ...(item.also || [])].flatMap(id => [...elementsFor(id)])) {
        const target = el.closest('[class*="_fieldEntry"],.ashby-application-form-field-entry') || el;
        target.style.outline = '3px solid #b3372f';
        target.style.outlineOffset = '4px';
      }
    };
    const answered = {count: 0};
    const render = item => {
      const row = make('div', 'item');
      row.append(press(make('p', 'label', item.label), () => elementsFor(item.field_id)[0]?.scrollIntoView({block: 'center'})));
      if (item.description) row.append(make('p', 'why', item.description));
      row.append(make('p', 'reason', item.reason));
      if (item.repeated) row.append(make('p', 'why', '这个标题在表上出现了好几次，答案不能记。请在这页上手动填。'));
      const save = async answer => {
        const reply = await send({type: 'agentAnswer', body: {question: item.label, description: item.description || '', answer, job_id: item.job_id}});
        if (!reply?.ok) { status.textContent = '没记住：' + (reply?.error || '连接断了，请到 8787 看板回答'); return; }
        answered.count++;
        controls.replaceChildren(make('p', 'done', '✓ 记住了：' + answer));
        status.textContent = '';
      };
      const controls = make('div', 'row'), status = make('p', 'reason');
      if (item.answerable && item.options.length && item.options.length <= 12) {
        for (const option of item.options) controls.append(press(make('button', '', option), () => save(option)));
      } else if (item.answerable) {
        const input = make('textarea');
        input.rows = 2;
        // Shown, not prefilled: that text is the model's or the page's, and one click would make it her answer for every company.
        if (item.value) row.append(make('p', 'why', '页面上现在是：' + [...String(item.value)].slice(0, 200).join('')));
        controls.append(input, press(make('button', 'primary', '记住'), () => { if (input.value.trim()) save(input.value.trim()); }));
      }
      row.append(controls, status);
      return row;
    };
    for (const item of items) { outline(item); box.append(render(item)); }
    for (const text of pageIssues) { const row = make('div', 'item'); row.append(make('p', 'reason', '答案解决不了：' + text)); box.append(row); }
    const nextStep = make('p', 'why', verification
      ? (mailcode ? '在这页输入验证码，然后再点一次提交。点右上角面板的「📧 Get email code」，它会读这家公司发来的验证码并填好。'
        : '去邮箱找这家公司发来的验证码，在这页输入，然后再点一次提交。') + '读到雇主的成功回执后，这份会自动记为已投。'
      : clicked ? '提交按钮已经点过了，只是没读到雇主的回执。先查邮箱和雇主的页面，不要在这页再提交，也不要重跑。'
      : '你也可以在这页自己填完并提交。读到雇主的成功回执后，这份会自动记为已投；没记上就点右上角面板的「I submitted this」。');
    const foot = make('div', 'foot');
    foot.append(press(make('button', 'primary', '答完了，去看板重跑这份'), () => send({type: 'openBoardAndClose'})));
    box.append(nextStep);
    if (!clicked) box.append(foot);
    (document.body || document.documentElement).append(host);
    elementsFor(items[0]?.field_id)[0]?.scrollIntoView({block: 'center'});
    return {
      // The employer confirmed what she sent by hand, so nothing here may invite another send.
      applied(text) {
        heading.textContent = '这份已经投出';
        nextStep.textContent = text;
        foot.remove();
      },
      // The employer turned down what she sent by hand. A re-run would only meet the same answer.
      refused(text) {
        heading.textContent = '雇主没有收这份';
        nextStep.textContent = text;
        foot.remove();
      },
      note(text) { box.append(make('p', 'reason', text)); },
    };
  }
  globalThis.JobScanStopPanel = {show, rowsFrom};
})();
