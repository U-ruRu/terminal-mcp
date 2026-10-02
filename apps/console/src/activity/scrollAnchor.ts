export type ActivityScrollAnchor = {
  sourceSeq?: string
  offset?: number
  scrollHeight: number
}

const LOGICAL_ITEM_SELECTOR = '[data-activity-source-seqs]'

function sourceSeqs(element: HTMLElement): string[] {
  return (element.dataset.activitySourceSeqs ?? '').trim().split(/\s+/).filter(Boolean)
}

export function captureActivityScrollAnchor(node: HTMLDivElement): ActivityScrollAnchor {
  const nodeTop = node.getBoundingClientRect().top
  const firstVisible = [...node.querySelectorAll<HTMLElement>(LOGICAL_ITEM_SELECTOR)]
    .find((element) => element.getBoundingClientRect().bottom > nodeTop)

  if (!firstVisible) return { scrollHeight: node.scrollHeight }

  return {
    sourceSeq: sourceSeqs(firstVisible).at(-1),
    offset: firstVisible.getBoundingClientRect().top - nodeTop,
    scrollHeight: node.scrollHeight,
  }
}

export function restoreActivityScrollAnchor(node: HTMLDivElement, anchor: ActivityScrollAnchor): void {
  const nodeTop = node.getBoundingClientRect().top
  const target = anchor.sourceSeq
    ? [...node.querySelectorAll<HTMLElement>(LOGICAL_ITEM_SELECTOR)]
      .find((element) => sourceSeqs(element).includes(anchor.sourceSeq!))
    : undefined

  const delta = target && anchor.offset !== undefined
    ? target.getBoundingClientRect().top - nodeTop - anchor.offset
    : node.scrollHeight - anchor.scrollHeight

  if (Math.abs(delta) > 0.5) node.scrollTop += delta
}
