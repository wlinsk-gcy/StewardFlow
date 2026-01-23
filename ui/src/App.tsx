import './App.css'
import {AgentWorkbench} from "./components/AgentWorkbench.tsx";


export default function App() {
    return (
        <div className="h-screen w-full bg-[#f8f9fc] p-4 flex items-center justify-center">
            <div className="w-full h-[calc(100vh-2rem)] max-w-[1600px] flex flex-col">
              <AgentWorkbench />
            </div>
        </div>
    );
}
