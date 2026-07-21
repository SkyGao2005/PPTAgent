/**
 * Fixed full-viewport backdrop for the liquid-glass design: a warm paper
 * gradient with soft pastel color fields, plus a few blurred orbs that the
 * glass surfaces refract.
 */
export function SceneBackground() {
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none fixed inset-0 z-0 overflow-hidden"
      style={{ filter: "saturate(0.90)" }}
    >
      <div
        className="liquid-scene-base absolute -inset-[18%]"
        style={{
          background:
            "radial-gradient(68% 82% at 12% 18%, rgba(232,204,214,0.34) 0%, rgba(232,204,214,0.12) 48%, transparent 80%)," +
            "radial-gradient(64% 78% at 88% 12%, rgba(165,213,255,0.46) 0%, rgba(165,213,255,0.16) 46%, transparent 78%)," +
            "radial-gradient(66% 80% at 78% 86%, rgba(255,214,182,0.4) 0%, rgba(255,214,182,0.14) 50%, transparent 80%)," +
            "radial-gradient(62% 76% at 18% 84%, rgba(178,235,214,0.43) 0%, rgba(178,235,214,0.15) 48%, transparent 79%)," +
            "linear-gradient(180deg, #f7f6f4, #efedeb)",
        }}
      />
      <div
        className="liquid-scene-orb liquid-scene-orb--blue absolute top-[42%] left-[1%] size-[620px] rounded-full"
        style={{
          background:
            "radial-gradient(circle at 35% 35%, rgba(147,197,253,0.48) 0%, rgba(147,197,253,0.2) 52%, rgba(147,197,253,0) 90%)",
        }}
      />
      <div
        className="liquid-scene-orb liquid-scene-orb--rose absolute top-[2%] right-[1%] size-[560px] rounded-full"
        style={{
          background:
            "radial-gradient(circle at 40% 40%, rgba(235,204,213,0.32) 0%, rgba(235,204,213,0.12) 54%, rgba(235,204,213,0) 91%)",
        }}
      />
      <div
        className="liquid-scene-orb liquid-scene-orb--peach absolute right-[14%] bottom-[-4%] size-[500px] rounded-full"
        style={{
          background:
            "radial-gradient(circle at 40% 40%, rgba(253,215,170,0.4) 0%, rgba(253,215,170,0.16) 52%, rgba(253,215,170,0) 90%)",
        }}
      />
      <div
        className="liquid-scene-orb liquid-scene-orb--mint absolute top-[-10%] left-[12%] size-[540px] rounded-full"
        style={{
          background:
            "radial-gradient(circle at 42% 38%, rgba(159,224,201,0.4) 0%, rgba(159,224,201,0.15) 54%, rgba(159,224,201,0) 91%)",
        }}
      />
      <div className="liquid-scene-sheen" />
    </div>
  )
}
