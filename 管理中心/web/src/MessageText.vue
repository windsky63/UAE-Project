<script setup>
import { copyText } from './browser.js'
import {computed} from 'vue'
const props=defineProps({text:{type:String,default:''}})
const blocks=computed(()=>{
  const chunks=props.text.split(/```/)
  return chunks.flatMap((chunk,i)=>{
    if(i%2){const n=chunk.indexOf('\n');return[{type:'code',language:n>=0?chunk.slice(0,n):'',text:n>=0?chunk.slice(n+1):chunk}]}
    return chunk.split('\n').map(line=>({type:line.startsWith('### ')?'h3':line.startsWith('## ')?'h2':/^[-*] /.test(line)?'li':'text',text:line.replace(/^(#{2,3} |[-*] )/,'')}))
  })
})
function parts(text){return text.split(/(`[^`]+`|\*\*[^*]+\*\*)/).map(t=>t.startsWith('`')?{type:'code',text:t.slice(1,-1)}:t.startsWith('**')?{type:'strong',text:t.slice(2,-2)}:{type:'span',text:t})}
async function copy(text){try{await copyText(text)}catch{}}
</script>
<template><div class="codex-markdown"><template v-for="(block,i) in blocks" :key="i"><div v-if="block.type==='code'" class="codex-code"><div class="codex-code-bar"><span>{{block.language||'text'}}</span><button aria-label="复制代码" @click="copy(block.text)"><v-icon icon="mdi-content-copy" size="14"/></button></div><pre><code>{{block.text}}</code></pre></div><component v-else :is="block.type==='text'?'p':block.type" :class="{'codex-list-line':block.type==='li'}"><component v-for="(part,j) in parts(block.text)" :key="j" :is="part.type">{{part.text}}</component><br v-if="!block.text"/></component></template></div></template>
