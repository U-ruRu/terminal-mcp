export function Placeholder({ title }: { title: string }) {
  return (
    <section className="stack">
      <div className="page-heading">
        <div><p className="eyebrow">Single-server view</p><h2>{title}</h2></div>
      </div>
      <article className="panel">
        <h3>{title} read model</h3>
        <p className="muted">Route is scaffolded and ready for typed M1 read models.</p>
      </article>
    </section>
  )
}
