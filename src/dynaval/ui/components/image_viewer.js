export default {
  template: `<div style="position:absolute;inset:0;overflow:hidden;touch-action:none" @wheel.prevent="wheel"
      @pointerdown="start" @pointermove="move" @pointerup="end" @pointercancel="end" @dblclick="reset">
    <img :src="source" :alt="label" draggable="false"
      :style="{width:'100%',height:'100%',objectFit:'contain',userSelect:'none',transform:'translate('+x+'px,'+y+'px) scale('+zoom+')',cursor:dragging?'grabbing':'grab'}" />
    <div style="position:absolute;bottom:18px;left:50%;transform:translateX(-50%);display:flex;gap:4px;background:#fff;border:1px solid #dfe8ea;border-radius:10px;padding:4px;box-shadow:0 4px 20px #10193512" @pointerdown.stop @wheel.stop>
      <q-btn flat dense icon="remove" aria-label="Zoom out" @click="scale(.8)" />
      <q-btn flat dense :label="Math.round(zoom*100)+'%'" aria-label="Reset zoom" @click="reset" />
      <q-btn flat dense icon="add" aria-label="Zoom in" @click="scale(1.25)" />
      <q-btn flat dense icon="fit_screen" aria-label="Fit image" @click="reset" />
    </div>
  </div>`,
  props: {source:String,label:String},
  data() { return {zoom:1,x:0,y:0,dragging:false,startX:0,startY:0}; },
  watch: {source() {this.reset();}},
  methods: {
    reset(){this.zoom=1;this.x=0;this.y=0;},
    scale(factor){this.zoom=Math.min(12,Math.max(.25,this.zoom*factor));},
    wheel(event){this.scale(event.deltaY<0?1.12:1/1.12);},
    start(event){if(event.button!==0)return;this.dragging=true;this.startX=event.clientX-this.x;this.startY=event.clientY-this.y;event.currentTarget.setPointerCapture(event.pointerId);},
    move(event){if(this.dragging){this.x=event.clientX-this.startX;this.y=event.clientY-this.startY;}},
    end(){this.dragging=false;}
  }
};
