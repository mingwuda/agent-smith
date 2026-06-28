import { useMemo } from "react";
import { useStepper } from "./hooks/useStepper";
import { useAutoMode } from "./hooks/useAutoMode";
import { CHAPTERS } from "./registry/chapters";
import { Stage } from "./components/Stage";
import { ProgressBar } from "./components/ProgressBar";
import { AutoStartGate } from "./components/AutoStartGate";
import { AutoToggle } from "./components/AutoToggle";

export default function App() {
const { mode, cycleMode, autoStarted } = useAutoMode();
const { cursor, next, prev, jumpToChapter, totalGlobal, chapterTotalSteps } =
useStepper(CHAPTERS);

const chapter = useMemo(() => CHAPTERS[cursor.chapter], [cursor.chapter]);
if (!chapter) return null;

const Component = chapter.Component;
const narration = chapter.narrations[cursor.step] ?? "";
const audioSrc = narration ? `/audio/${chapter.id}/${cursor.step}.mp3` : null;

return (
<div className="app-shell">
<Stage onAdvance={next}>
<Component step={cursor.step} />
</Stage>

<ProgressBar
chapters={CHAPTERS}
cursor={cursor}
onJumpChapter={(idx, step) => jumpToChapter(idx, step)}
/>

<AutoToggle mode={mode} onCycle={cycleMode} />
<AutoStartGate visible={mode === "auto" && !autoStarted} onStart={cycleMode} />
</div>
);
}
