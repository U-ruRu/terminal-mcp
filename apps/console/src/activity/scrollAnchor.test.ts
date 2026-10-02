import { expect, test } from 'vitest'

import { captureActivityScrollAnchor, restoreActivityScrollAnchor } from './scrollAnchor'

function rect(top: number, bottom: number): DOMRect {
  return { top, bottom, left: 0, right: 320, width: 320, height: bottom - top, x: 0, y: top, toJSON: () => ({}) } as DOMRect
}

test('anchors prepend to the first visible logical item, never the date separator', () => {
  const scroller = document.createElement('div') as HTMLDivElement
  const separator = document.createElement('div')
  separator.dataset.activityKey = 'date:2026-10-02'
  const message = document.createElement('article')
  message.dataset.activityKey = 'event:102'
  message.dataset.activitySourceSeqs = '101 102'
  scroller.append(separator, message)

  Object.defineProperty(scroller, 'scrollHeight', { configurable: true, value: 500 })
  scroller.scrollTop = 20
  scroller.getBoundingClientRect = () => rect(100, 500)
  separator.getBoundingClientRect = () => rect(100, 118)
  message.getBoundingClientRect = () => rect(118, 170)

  const anchor = captureActivityScrollAnchor(scroller)
  expect(anchor).toEqual({ sourceSeq: '102', offset: 18, scrollHeight: 500 })
})

test('restores the same raw source event after prepend even when semantic item key changes', () => {
  const scroller = document.createElement('div') as HTMLDivElement
  const message = document.createElement('article')
  message.dataset.activityKey = 'event:80'
  message.dataset.activitySourceSeqs = '80 101 102'
  scroller.append(message)

  Object.defineProperty(scroller, 'scrollHeight', { configurable: true, value: 760 })
  scroller.scrollTop = 20
  scroller.getBoundingClientRect = () => rect(100, 500)
  message.getBoundingClientRect = () => rect(318, 370)

  restoreActivityScrollAnchor(scroller, { sourceSeq: '102', offset: 18, scrollHeight: 500 })
  expect(scroller.scrollTop).toBe(220)
})

test('falls back to scroll-height preservation when the semantic anchor disappears', () => {
  const scroller = document.createElement('div') as HTMLDivElement
  Object.defineProperty(scroller, 'scrollHeight', { configurable: true, value: 740 })
  scroller.scrollTop = 30
  scroller.getBoundingClientRect = () => rect(100, 500)

  restoreActivityScrollAnchor(scroller, { sourceSeq: '102', offset: 18, scrollHeight: 500 })
  expect(scroller.scrollTop).toBe(270)
})
