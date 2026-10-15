// Shared coordinator for fixture and Chrome. Reviewer owns submit; filler only writes fields.
(() => {
  async function run(io, {autoSubmit = false, maxRounds = 5} = {}) {
    let current = null, attempts = 0;
    const emit = (role, text, detail = {}) => io.event?.({role, text, ...detail});
    const unchanged = s => JSON.stringify(io.capture()) === JSON.stringify(s);
    // By code point: half an emoji is a lone surrogate, which the local service rejects outright.
    const cut = (value, max) => [...String(value)].slice(0, max).join('');
    // What the form actually holds now, read back from the page rather than the filler's log.
    const written = s => s.fields.filter(f => String(f.value).trim())
      .map(f => `${cut(f.label, 60)}：${cut(f.value, 80)}`).join('\n');
    emit('filler', '读取当前身份和确认事实，开始填表。', {stage:'fill'});
    const filled = await io.fill();
    if (filled?.status !== 'completed') return {status:'needs_user', reason:`填表未完成：${filled?.error || filled?.status || '填表组件未返回结果'}`};
    emit('filler', `填写结束，表单现在的内容：\n${written(io.capture()) || '（没有读到已填内容）'}`, {stage:'fill', to:'reviewer'});
    for (let n = 0; n < maxRounds; n++) {
      await io.prepare?.();
      const snapshot = io.capture();
      emit('reviewer', `第 ${n + 1} 轮：独立读取 ${snapshot.fields.length} 个字段。`, {stage:'review'});
      current = await io.review(snapshot, current);
      const r = current.result;
      emit('reviewer', r.summary, {issues:r.issues, snapshot, result:r});
      // Nothing was clicked yet, so a moving page only voids this round's verdict.
      if (!unchanged(snapshot)) { emit('reviewer', '检查期间页面有变化，这一轮结论作废，重新读取。', {stage:'review'}); continue; }
      if (r.model_status !== 'completed') return {status:'needs_user', reason:r.summary, run:current};
      if (r.decision !== 'pass') {
        const fixes = r.issues.filter(x => x.auto_fix);
        if (!fixes.length && io.planRepair && n < maxRounds - 1) {
          emit('filler', '收到审核反馈，独立依据已确认事实生成修正方案。', {stage:'repair'});
          const plan = await io.planRepair(snapshot, current);
          emit('filler', plan.summary, {stage:'repair'});
          if (plan.status === 'planned') fixes.push(...plan.repairs);
        }
        // Unknown facts never become guesses just because a retry is available.
        if (r.model_status !== 'completed' || !fixes.length || n === maxRounds - 1)
          return {status:'needs_user', reason:r.summary, run:current};
        emit('reviewer', '反馈给 Filler：按下列确认事实修正，修正后交回审查。', {stage:'repair', issues:fixes, snapshot, to:'filler'});
        const changes = await io.repair(snapshot, fixes);
        emit('filler', changes.map(x => `${x.label}：${x.before || '空白'} → ${x.after}`).join('\n'), {stage:'repair', to:'reviewer'});
        continue;
      }
      current = await io.confirm(snapshot, current);
      if (!unchanged(snapshot)) { emit('reviewer', '确认期间页面有变化，原通过结论作废，重新读取。', {stage:'review'}); continue; }
      if (!autoSubmit) return {status:'review_passed', run:current, snapshot};
      if (attempts >= 2) return {status:'needs_user', reason:'两次提交尝试已用完，请核对原申请。', run:current};
      const gate = io.preflight(snapshot);
      if (!gate.ok) return {status:/^雇主拒收/.test(gate.reason) ? 'blocked' : 'needs_user', reason:gate.reason, run:current};
      current = await io.permit(snapshot, current);
      if (!unchanged(snapshot)) {
        await io.report({outcome:'blocked', evidence:'提交前页面改变；没有点击按钮。'}, current);
        throw new Error('提交前页面改变，未点击。');
      }
      emit('reviewer', '检查通过，点击本份申请的 Submit，等待雇主回执。', {stage:'submit'});
      attempts++;
      let outcome;
      try { outcome = await io.submit(snapshot); }
      catch { outcome = {outcome:'unknown', evidence:'点击阶段发生异常，无法确认是否已提交；停止重试。'}; }
      current = await io.report(outcome, current);
      emit('reviewer', outcome.evidence, {stage:outcome.outcome === 'submitted' ? 'done' : 'repair'});
      if (outcome.outcome === 'submitted') {
        // Recording failures must not lead to another employer submission.
        try { await io.record?.(current); }
        catch { return {status:'submitted', reason:'雇主确认成功，但 JobScan 记账失败；不要重投。', run:current}; }
        return {status:'submitted', reason:outcome.evidence, run:current};
      }
      // "unknown" may have reached the employer, so it must end a batch; a plain stop must not.
      if (outcome.outcome !== 'validation_error') return {status:['unknown','blocked'].includes(outcome.outcome) ? outcome.outcome : 'needs_user', reason:outcome.evidence, run:current};
      emit('reviewer', '提交被字段校验拒绝。反馈给 Filler，重新读取错误并审查；修正后最多再试一次。', {stage:'review'});
    }
    return {status:'needs_user', reason:'已达到五轮审查上限。', run:current};
  }
  globalThis.JobScanWorkflow = {run};
})();
