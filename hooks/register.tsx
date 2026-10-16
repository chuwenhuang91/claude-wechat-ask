import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { Pending } from '../types'

// 与 MCP server 约定的交接目录；$.env.get 的参数必须是字符串字面量
const stateDir = async ($: { env: { get: (n: string) => Promise<string | undefined> } }) => {
  const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME')) ?? '.'

  return `${home}/.wechat-ask`
}

const SVG_WIDTH = 620
const SVG_PAD_LEFT = 14
const H_SIZE = 12
const H_LINE = 14
const Q_SIZE = 20
const Q_LINE = 25
const O_SIZE = 16
const O_LINE = 20
const O_GAP = 4
// 半角算 1、全角算 2。620px 下 20px 字约放 31 个全角，16px 约放 38 个
const Q_MAX_UNITS = 60
const O_MAX_UNITS = 74

const XML_ESCAPES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&apos;',
}

const escapeXml = (s: string) => s.replace(/[&<>"']/g, c => XML_ESCAPES[c] ?? c)

const wrapByWidth = (text: string, maxUnits: number) => {
  const lines: string[] = []
  let line = ''
  let units = 0
  for (const ch of text) {
    const w = /[\x00-\xff]/.test(ch) ? 1 : 2
    if (units + w > maxUnits && line) {
      lines.push(line)
      line = ''
      units = 0
    }
    line += ch
    units += w
  }
  if (line) {
    lines.push(line)
  }

  return lines
}

const cardSvg = (question: string, options: string[], held: boolean) => {
  const parts: string[] = []
  const x = SVG_PAD_LEFT
  let y = H_LINE

  // 提示行也画进 SVG：AbovePrompt 的高度有限，少一个元素就少一截行距
  const hint = held ? '已取消微信推送，慢慢想' : '远程询问 · 不回答会转到微信'
  parts.push(`<text class="h" x="${x}" y="${y}" font-size="${H_SIZE}">${escapeXml(hint)}</text>`)

  for (const line of wrapByWidth(question, Q_MAX_UNITS)) {
    y += Q_LINE
    parts.push(`<text class="q" x="${x}" y="${y}" font-size="${Q_SIZE}">${escapeXml(line)}</text>`)
  }

  options.forEach((option, i) => {
    y += O_GAP
    wrapByWidth(`${i + 1}. ${option}`, O_MAX_UNITS).forEach((line, j) => {
      y += O_LINE
      parts.push(
        `<text class="o" x="${x + (j === 0 ? 0 : 22)}" y="${y}" font-size="${O_SIZE}">${escapeXml(line)}</text>`,
      )
    })
  })

  // 底部多留一截：这是选项和下面那排按钮之间的空白，走 SVG 像素比用 gap 占一整行省
  const height = y + 16
  // 不用 isInteractive：那会放进 iframe，自带白底还会滚动。按图片渲染背景是透明的，
  // 代价是 @media(prefers-color-scheme) 不生效，颜色只能写死。
  // AbovePrompt 这块区域实际是深色底，和 ~/.claude.json 里的 theme=light 不是一回事
  const style =
    'text{font-family:"Microsoft YaHei UI","Segoe UI",sans-serif}' +
    '.h{fill:#9a9794}' +
    '.q{fill:#ffffff;font-weight:600}' +
    '.o{fill:#d4d2cf}'

  return `<svg xmlns="http://www.w3.org/2000/svg" width="${SVG_WIDTH}" height="${height}" viewBox="0 0 ${SVG_WIDTH} ${height}"><style>${style}</style><rect x="0" y="0" width="3" height="${height - 12}" rx="1.5" fill="#d97757"/>${parts.join('')}</svg>`
}

const pending = atom({ plugin: 'wechat-ask', key: 'pending' } as const, null as Pending)

export const register: Register = on => {

  on('tool.call', async ($, e, next) => {
    if (!String(e.tool).includes('ask_remote')) {
      return next(e)
    }

    const args = e as unknown as Record<string, unknown>
    const question = String(args.question ?? '')
    if (!question || question.startsWith('__debug')) {
      return next(e)
    }

    const options = Array.isArray(args.options) ? args.options.map(String) : []
    const waitSeconds = Math.abs(Number(args.local_wait_seconds ?? 30))

    await update($, pending, () => ({ question, options }))
    try {
      // 不在这里等人回答：hook 每次 dispatch 只有 10 秒预算，超了引擎就判定它
      // 缺席并替它放行。next() 飞行期间预算暂停，所以等待交给 MCP server——
      // 它去轮询 pre-answer.json，没有时间限制。负数是给它的信号。
      return await next({ ...e, local_wait_seconds: -waitSeconds })
    } finally {
      await update($, pending, () => null)
    }
  })

  // 落盘必须在 hook 里 await 完成：onPress / onSubmit 闭包里起的异步任务会随
  // dispatch 结束被丢掉，文件根本写不出去。
  on('ui.press', async ($, e, next) => {
    const ask = await read($, pending)
    const element = String(e.element ?? '')

    if (ask && element === 'dismiss') {
      await $.fs.write(
        `${await stateDir($)}/pre-answer.json`,
        JSON.stringify({ cancelled: true, at: await $.clock.now() }),
      )
      await update($, pending, () => null)

      return next(e)
    }

    // 「我在想」：留个信号让 MCP server 放弃推微信，只等本地回答
    if (ask && element === 'hold') {
      await $.fs.write(
        `${await stateDir($)}/hold.json`,
        JSON.stringify({ at: await $.clock.now() }),
      )
      await update($, pending, current => (current ? { ...current, held: true } : current))

      return next(e)
    }

    const match = /^option-(\d+)$/.exec(element)
    const picked = ask && match ? ask.options[Number(match[1])] : undefined
    if (picked) {
      await $.fs.write(
        `${await stateDir($)}/pre-answer.json`,
        JSON.stringify({ answer: picked, at: await $.clock.now() }),
      )
      await update($, pending, () => null)
    }

    return next(e)
  })

  on('ui.input', async ($, e, next) => {
    const text = e.kind === 'submit' && e.element === 'answer' ? e.value.trim() : ''
    if (text) {
      // 输入纯数字时当作选项序号，省得去点那排小按钮
      const ask = await read($, pending)
      const index = /^\d+$/.test(text) ? Number(text) - 1 : -1
      const answer = ask && index >= 0 && index < ask.options.length ? ask.options[index] : text

      await $.fs.write(
        `${await stateDir($)}/pre-answer.json`,
        JSON.stringify({ answer, at: await $.clock.now() }),
      )
      await update($, pending, () => null)
    }

    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const ask = await read($, pending)
    if (!ask) {
      return next(e)
    }

    const table = $.ui.resolve(e)
    const { Box, Button, Input, Text } = table
    // Svg 只在 desktop 的元素表里；别的 surface 退回普通文本
    const Svg = (table as Record<string, unknown>).Svg as typeof Box | undefined

    return (
      <Box flexDirection="column" paddingX={1}>
        {Svg ? (
          <Svg
            source={cardSvg(ask.question, ask.options, Boolean(ask.held))}
            alt={[ask.question, ...ask.options.map((o, i) => `${i + 1}. ${o}`)].join('\n')}
          />
        ) : (
          <Box flexDirection="column">
            <Text bold>{ask.question}</Text>
            {ask.options.map((option, i) => (
              <Text>{`${i + 1}. ${option}`}</Text>
            ))}
          </Box>
        )}

        <Box alignItems="center" gap={1} marginBottom={1}>
          {ask.options.map((option, i) => (
            <Button
              key={`option-${i}`}
              // 半角空格会被裁掉，用全角空格（U+3000）撑宽，它本身也占两列
              label={Svg ? `　　${i + 1}　　` : `${i + 1}. ${option}`}
              variant={i === 0 ? 'primary' : 'secondary'}
              onPress={() => {}}
            />
          ))}
          {!ask.held && (
            <Button key="hold" label="我在想，别发微信" variant="secondary" onPress={() => {}} />
          )}
          <Button key="dismiss" role="dismiss" label="关闭" onPress={() => {}} />
        </Box>

        <Input key="answer" placeholder="输入序号或直接回答，回车提交" autoFocus onSubmit={() => {}} />
      </Box>
    )
  })
}
